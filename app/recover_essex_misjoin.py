#!/usr/bin/env python3
"""
One-off recovery for the Essex Apollo mis-join (2026-06-11).

THE BUG
  The 8-June Companies House re-fetch (corporate-filter fix, ~20,572 → 50,059
  rows) renumbered every Unique ID, because merge.py used to assign IDs by row
  order. Two of the three Apollo return files were keyed to the OLD pre-refetch
  masters and were then ingested BY Unique ID into the renumbered master, so
  ~51% of email rows landed enrichment on the WRONG person.

  - return_batch2_send6700.csv  → cut AFTER the re-fetch → correctly keyed → fine.
  - return_batch1_uid0001-8441  → keyed to _archive_june5_buggy/master_ESSEX_raw.csv
  - return_batch3_uid10000plus  → keyed to _archive_june5_buggy/master_ESSEX_raw_batch2.csv

THE FIX (no new Apollo spend — re-places already-purchased enrichment)
  1. Quarantine the corrupt enriched/classified/deliverable artefacts.
  2. Translate each old return's stale UID  → person identity (via the archived
     old master that produced it)  → the person's CURRENT Unique ID.
  3. Re-ingest all three returns cleanly from master_ESSEX_raw.csv through the
     now identity-verified ingest_multiple (which itself rejects any residual
     wrong-person UID).

After this: re-run Stage 5 (classify) and Stage 8 (export).

Usage (from app/):  python recover_essex_misjoin.py
"""
import os
import csv
import shutil

from stages.apollo_ingest import ingest_multiple

PROJ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", "6_ESSEX")
RC = "ESSEX"
QUAR = os.path.join(PROJ, "_corrupt_misjoin_2026-06-11")
REKEY_DIR = os.path.join(PROJ, "_recovery_rekeyed")

CURRENT_RAW = "master_ESSEX_raw.csv"
OLD_MASTERS = [
    "_archive_june5_buggy/master_ESSEX_raw.csv",          # old #ESSEX-0001..8441
    "_archive_june5_buggy/master_ESSEX_raw_batch2.csv",   # old #ESSEX-8442..20572
]
OLD_KEYED = [   # returns keyed to the OLD masters — must be re-keyed
    "apollo_returns/return_batch1_uid0001-8441.csv",
    "apollo_returns/return_batch3_uid10000plus.csv",
]
CURRENT_KEYED = [   # returns already keyed to the CURRENT master — ingest as-is
    "apollo_returns/return_batch2_send6700.csv",
]


def _idkey(surname, first, dob, company_number):
    return "|".join(str(x).strip().lower()
                    for x in (surname, first, dob, company_number))


def _load(path):
    with open(os.path.join(PROJ, path), newline="") as fh:
        return list(csv.DictReader(fh))


def main():
    # ── 1. Quarantine the corrupt artefacts ──────────────────────────────────
    os.makedirs(QUAR, exist_ok=True)
    for name in ("master_ESSEX_enriched.csv",
                 "master_ESSEX_classified.csv",
                 "ESSEX_final_deliverable.xlsx"):
        src = os.path.join(PROJ, name)
        if os.path.exists(src):
            shutil.move(src, os.path.join(QUAR, name))
            print(f"quarantined {name} -> {os.path.basename(QUAR)}/")
    lock = os.path.join(PROJ, "~$ESSEX_final_deliverable.xlsx")
    if os.path.exists(lock):
        os.remove(lock)

    # ── 2. Build old-UID → current-UID translation ───────────────────────────
    old_uid_to_id = {}
    for m in OLD_MASTERS:
        for r in _load(m):
            u = (r.get("Unique ID") or "").strip()
            if u:
                old_uid_to_id[u] = _idkey(r.get("Surname"), r.get("First Name"),
                                          r.get("Officer date of birth"),
                                          r.get("Company Number"))
    id_to_current = {}
    for r in _load(CURRENT_RAW):
        k = _idkey(r.get("Surname"), r.get("First Name"),
                   r.get("Officer date of birth"), r.get("Company Number"))
        id_to_current.setdefault(k, (r.get("Unique ID") or "").strip())
    old_to_current = {u: id_to_current[i]
                      for u, i in old_uid_to_id.items() if i in id_to_current}
    print(f"\ntranslation map: {len(old_to_current):,} old UIDs → current "
          f"(of {len(old_uid_to_id):,} known in old masters)")

    # ── 3. Re-key the old returns into temp copies ───────────────────────────
    os.makedirs(REKEY_DIR, exist_ok=True)
    rekeyed = []
    for path in OLD_KEYED:
        rows = _load(path)
        fields = list(rows[0].keys())
        translated = untranslated = 0
        out = os.path.join(REKEY_DIR, os.path.basename(path))
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows:
                old = (r.get("Unique ID") or "").strip()
                new = old_to_current.get(old, "")
                # Blank the UID when untranslatable so ingest falls back to name
                # match / orphans it, rather than trusting a stale ID.
                r["Unique ID"] = new
                w.writerow(r)
                translated += 1 if new else 0
                untranslated += 0 if new else 1
        print(f"  {os.path.basename(path)}: re-keyed {translated:,}, "
              f"{untranslated:,} untranslatable (blanked)")
        rekeyed.append(out)

    # ── 4. Re-ingest from the clean raw master ───────────────────────────────
    # Current-keyed (most trusted) first so it wins the blank-fill, then the
    # re-keyed old returns fill remaining blanks.
    files = [os.path.join(PROJ, p) for p in CURRENT_KEYED] + rekeyed
    print("\nRe-ingesting through identity-verified ingest_multiple:")
    res = ingest_multiple(PROJ, RC, files, progress_cb=lambda m: print("   ", m))
    print("\nresult:", res)
    print("\nNext: re-run Stage 5 (classify) then Stage 8 (export).")


if __name__ == "__main__":
    main()
