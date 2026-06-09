"""
Corrected re-fetch of all 14 Essex postcodes, after the _is_corporate fix.

Why: the old Stage-1 filter dropped every officer with a middle name (see the
ch-fetch-corporate-filter-bug note), so every existing results_*.csv is ~3x
undercounted. This re-runs the fetch with the fixed filter to recover the
missing real people.

Safety:
  - Writes to projects/6_ESSEX/postcodes_refetch/ — does NOT touch the existing
    postcodes/ data, which is load-bearing for the already-enriched batch 1 +
    first-10k and the assigned Unique IDs. Nothing here renumbers or re-enriches;
    it only produces the corrected raw fetch so we can size the delta.
  - Resumable: an area whose output already exists is skipped, so a crash/restart
    continues where it left off.
  - One process, sequential — the CH rate limiter is process-wide, so this keeps
    us inside the 600/5min budget. Do NOT run a second fetch against the same key
    concurrently.

Run (from app/, in the background — it takes hours):
  python refetch_essex.py
"""
import os
import glob
import time

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from stages.ch_fetch import fetch_postcode

# Smallest-first (by old row count) so a full corrected postcode + real
# multiplier lands within ~10 min as an early sanity check before the long haul.
AREAS = ["RM20", "CM21", "SS16", "RM18", "SS15", "CO1", "SS4",
         "SS2", "CM2", "CO4", "RM17", "CM20", "CM1", "SS14"]

PROJECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", "6_ESSEX")
OUT_DIR = os.path.join(PROJECT, "postcodes_refetch")


def _old_count(area):
    """Row count of the existing (buggy) fetch for an area, batch-1 or batch-2."""
    for cand in (os.path.join(PROJECT, "postcodes", f"results_{area}.csv"),
                 os.path.join(PROJECT, "postcodes", "_batch1", f"results_{area}.csv")):
        if os.path.exists(cand):
            with open(cand) as fh:
                return sum(1 for _ in fh) - 1
    return None


def main():
    key = os.getenv("CH_API_KEY")
    if not key:
        raise SystemExit("CH_API_KEY not set in app/.env")
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"Re-fetching {len(AREAS)} Essex postcodes → {OUT_DIR}", flush=True)
    print("(existing postcodes/ left untouched)\n", flush=True)

    results = {}
    for i, area in enumerate(AREAS, 1):
        out_path = os.path.join(OUT_DIR, f"results_{area}.csv")
        if os.path.exists(out_path):
            n = sum(1 for _ in open(out_path)) - 1
            print(f"[{i}/{len(AREAS)}] {area}: already done ({n:,} rows) — skipping", flush=True)
            results[area] = n
            continue

        print(f"[{i}/{len(AREAS)}] {area}: fetching...", flush=True)
        t0 = time.monotonic()
        try:
            n = fetch_postcode(area, key, out_path,
                               progress_cb=lambda m: print(f"      {m}", flush=True))
        except Exception as exc:
            print(f"   !! {area} FAILED: {exc}", flush=True)
            # leave no partial file behind so a rerun retries this area cleanly
            if os.path.exists(out_path):
                os.rename(out_path, out_path + ".partial")
            results[area] = None
            continue
        mins = (time.monotonic() - t0) / 60
        old = _old_count(area)
        delta = f"(was {old:,}, +{n - old:,})" if old is not None else ""
        print(f"   ✓ {area}: {n:,} people in {mins:.0f} min {delta}\n", flush=True)
        results[area] = n

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 56)
    print(f"{'area':6s} {'old':>9s} {'new':>9s} {'multiple':>9s}")
    print("-" * 56)
    to, tn = 0, 0
    for area in AREAS:
        new = results.get(area)
        old = _old_count(area)
        if new is None:
            print(f"{area:6s} {'-':>9s} {'FAILED':>9s}")
            continue
        to += old or 0
        tn += new
        mult = f"{new/old:.2f}x" if old else "n/a"
        print(f"{area:6s} {(old or 0):9,} {new:9,} {mult:>9s}")
    print("-" * 56)
    print(f"{'TOTAL':6s} {to:9,} {tn:9,} {(tn/to if to else 0):8.2f}x")
    print("=" * 56)


if __name__ == "__main__":
    main()
