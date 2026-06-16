"""
Build the OUTSTANDING Apollo upload queue — the people still needing enrichment.

Why this exists:
  Stage 3's magazine (apollo_batch_*.csv) is the full, person-deduped upload list,
  but it has no idea who's already been enriched. If we sent it straight to Apollo
  we'd re-pay for everyone Charles already did. This does the anti-join: it drops
  every magazine row whose Unique ID already carries an Apollo Email in the
  enriched master, leaving only people who genuinely still need a credit spent.

  Output: outstanding_<REGION>.csv (same 4 columns as the magazine). Feed it to
  credit_chop to size the next batch to the available credits.

Run (from app/):
  python build_send_queue.py            # defaults to ESSEX / projects/6_ESSEX
  python build_send_queue.py ESSEX
"""
import os
import glob
import sys

import pandas as pd

EMAIL_COL = "Apollo Email"  # the headline "this person is enriched" signal


def build_outstanding(project_dir, region_code, progress_cb=None):
    """
    Return the magazine rows for people NOT yet enriched, written to
    outstanding_<REGION>.csv.

    Returns dict: {candidates, already_enriched, outstanding, path}
    """
    log = progress_cb or print

    batch_files = sorted(glob.glob(os.path.join(project_dir, "apollo_batch_*.csv")))
    if not batch_files:
        raise FileNotFoundError(
            "No apollo_batch_*.csv found — build the magazine (Stage 3) first.")
    candidates = pd.concat(
        [pd.read_csv(f, dtype=str, keep_default_na=False) for f in batch_files],
        ignore_index=True,
    ).drop_duplicates(subset=["Unique ID"], keep="first")
    log(f"Magazine candidates: {len(candidates):,} unique people "
        f"({len(batch_files)} batch file(s))")

    # Who's already enriched? (blank-safe: only non-empty Apollo Email counts)
    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")
    enriched_uids = set()
    if os.path.exists(enriched_path):
        em = pd.read_csv(enriched_path, dtype=str, keep_default_na=False)
        if EMAIL_COL in em.columns:
            enriched_uids = set(em.loc[em[EMAIL_COL].str.strip() != "", "Unique ID"])
        log(f"Already enriched (excluded): {len(enriched_uids):,} people")
    else:
        log("No enriched master yet — nothing to exclude (everyone is outstanding)")

    outstanding = candidates[~candidates["Unique ID"].isin(enriched_uids)].copy()
    out_path = os.path.join(project_dir, f"outstanding_{region_code}.csv")
    outstanding.to_csv(out_path, index=False)

    log(f"Outstanding to enrich: {len(outstanding):,} → {os.path.basename(out_path)}")
    return {
        "candidates": len(candidates),
        "already_enriched": len(candidates) - len(outstanding),
        "outstanding": len(outstanding),
        "path": out_path,
    }


if __name__ == "__main__":
    region = sys.argv[1] if len(sys.argv) > 1 else "ESSEX"
    # Default project dir for the Essex recovery; adjust if reused elsewhere.
    pdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", "6_ESSEX")
    if not os.path.isdir(pdir):
        raise SystemExit(f"Project dir not found: {pdir}")
    res = build_outstanding(pdir, region)
    print("\n" + "=" * 52)
    print(f"  candidates       : {res['candidates']:,}")
    print(f"  already enriched : {res['already_enriched']:,}")
    print(f"  → outstanding    : {res['outstanding']:,}")
    print("=" * 52)
    print(f"  next: python -m stages.credit_chop {res['path']} <credits>")
