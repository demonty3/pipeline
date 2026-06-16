"""
Credit-aware file chopper — an operator helper for Charles.

What it does, in plain English:
  Charles has a CSV he wants to enrich in Apollo, but only N credits left.
  Apollo charges one credit per name (row), so the file he uploads must have
  no more names than he has credits. This trims any CSV/XLSX down so it fits:

    - Counts the names (rows) in the file.
    - Drops duplicate people first (same Unique ID = paying twice for nothing),
      so credits aren't wasted on noise.
    - Keeps the first `credits` names as the "send" file(s); writes everything
      beyond that to a `_deferred.csv` so the leftover queues for next time.
    - Also respects Apollo's hard 10,000-rows-per-upload cap: if Charles has
      more than 10k credits, the send set is split into several ≤10k files that
      together stay within his credit budget.

  Credit count can be passed in, or fetched live from Apollo (best-effort) when
  omitted — see stages.apollo_credits.

Why this exists (and isn't just Stage 3's credit_budget):
  Stage 3's build_batches caps the magazine while building it FROM a project
  master. This works the other way round: it takes a file Charles already holds
  (a magazine batch, the deferred file, a raw export Apollo emailed back) and
  chops THAT to fit. File-in / file-out, no project master required. It's what
  we needed when the Essex 10k upload blew past available credits.

Usage:
  # from the app/ directory
  python -m stages.credit_chop path/to/file.csv 4000      # explicit credits
  python -m stages.credit_chop path/to/file.csv           # auto-fetch credits
"""
import os
import math
import sys

import pandas as pd

APOLLO_FILE_CAP = 10_000  # Apollo's hard per-upload row cap


def _read_any(path):
    """Read a CSV or XLSX into a string-typed DataFrame (blanks stay blank)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str).fillna("")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def chop_for_credits(input_path, credits=None, out_dir=None,
                     file_cap=APOLLO_FILE_CAP, dedupe=True, progress_cb=None):
    """
    Trim an upload file so it never exceeds the available Apollo credits.

    Args:
        input_path: path to the CSV/XLSX Charles wants to enrich.
        credits: credits available. If None, fetched live from Apollo; if that
            also fails, raises (we won't guess a budget).
        out_dir: where to write outputs. Defaults to the input file's folder.
        file_cap: max rows per output file (Apollo's 10k cap; lower it to make
            more, smaller files).
        dedupe: drop duplicate people before counting (by Unique ID if present,
            else by fully-identical rows) so credits aren't wasted.
        progress_cb: optional callable(str) for progress lines.

    Returns:
        dict: {credits, input_rows, duplicates_dropped, sent_rows,
               deferred_rows, send_files: [{filename, row_count}, ...],
               deferred_file}
    """

    def log(msg):
        (progress_cb or print)(msg)

    if not os.path.exists(input_path):
        raise FileNotFoundError(input_path)

    # Resolve the credit budget.
    if credits is None:
        from stages.apollo_credits import fetch_credits_remaining
        credits = fetch_credits_remaining()
        if credits is None:
            raise ValueError(
                "Couldn't fetch Apollo credits automatically (no APOLLO_API_KEY, "
                "network issue, or unrecognised response). Pass the number in "
                "explicitly, e.g. chop_for_credits(path, credits=4000)."
            )
        log(f"Fetched {credits:,} credits remaining from Apollo")
    credits = int(credits)
    if credits <= 0:
        raise ValueError(f"Credits must be positive, got {credits}.")

    df = _read_any(input_path)
    input_rows = len(df)
    log(f"Loaded {os.path.basename(input_path)}: {input_rows:,} names")

    # ── Dedupe so credits aren't spent twice on the same person ───────────────
    duplicates_dropped = 0
    if dedupe and input_rows:
        before = len(df)
        # Dedup by Unique ID only when every row actually has one — otherwise a
        # file with a blank/absent ID column would collapse to a single row.
        if "Unique ID" in df.columns and (df["Unique ID"].astype(str).str.strip() != "").all():
            df = df.drop_duplicates(subset=["Unique ID"], keep="first")
        else:
            df = df.drop_duplicates(keep="first")
        duplicates_dropped = before - len(df)
        if duplicates_dropped:
            log(f"  Dropped {duplicates_dropped:,} duplicate name(s) "
                f"({len(df):,} unique names remain)")
    df = df.reset_index(drop=True)

    # ── Split into what fits the credit budget vs. what's deferred ────────────
    keep = min(credits, len(df))
    send_df = df.iloc[:keep]
    deferred_df = df.iloc[keep:]
    log(f"  Credits {credits:,}: sending {len(send_df):,}, "
        f"deferring {len(deferred_df):,}")

    out_dir = out_dir or os.path.dirname(os.path.abspath(input_path))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(input_path))[0]

    # ── Write the send file(s), each within Apollo's per-upload cap ───────────
    cap = max(1, min(file_cap, APOLLO_FILE_CAP))
    n_files = max(1, math.ceil(len(send_df) / cap)) if len(send_df) else 0
    send_files = []
    for i in range(n_files):
        chunk = send_df.iloc[i * cap:(i + 1) * cap]
        # One file → "<stem>_send.csv"; several → "<stem>_send_001.csv", ...
        name = f"{stem}_send.csv" if n_files == 1 else f"{stem}_send_{i + 1:03d}.csv"
        path = os.path.join(out_dir, name)
        chunk.to_csv(path, index=False)
        log(f"  → {name}: {len(chunk):,} names")
        send_files.append({"filename": name, "row_count": len(chunk)})

    # ── Write the leftover ────────────────────────────────────────────────────
    deferred_file = None
    if len(deferred_df):
        deferred_file = f"{stem}_deferred.csv"
        deferred_df.to_csv(os.path.join(out_dir, deferred_file), index=False)
        log(f"  → {deferred_file}: {len(deferred_df):,} names held for next run")

    return {
        "credits": credits,
        "input_rows": input_rows,
        "duplicates_dropped": duplicates_dropped,
        "sent_rows": len(send_df),
        "deferred_rows": len(deferred_df),
        "send_files": send_files,
        "deferred_file": deferred_file,
    }


def _cli(argv):
    if not argv:
        raise SystemExit(
            "usage: python -m stages.credit_chop <file.csv> [credits]\n"
            "  credits omitted → fetched live from Apollo"
        )
    path = argv[0]
    credits = int(argv[1]) if len(argv) > 1 else None
    result = chop_for_credits(path, credits=credits)
    print("\n" + "=" * 56)
    print(f"  input names      : {result['input_rows']:,}")
    print(f"  duplicates dropped: {result['duplicates_dropped']:,}")
    print(f"  credits          : {result['credits']:,}")
    print(f"  → sent           : {result['sent_rows']:,} "
          f"across {len(result['send_files'])} file(s)")
    print(f"  → deferred       : {result['deferred_rows']:,}")
    print("=" * 56)


if __name__ == "__main__":
    _cli(sys.argv[1:])
