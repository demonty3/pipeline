"""
One-off batch-2 Apollo ingester for the Essex project.

Why this exists (and isn't just the Stage 4 web button):
  Essex was split into two masters because the full 20,572-person upload was
  too big to run in one go:
    - master_ESSEX_raw.csv          batch 1  (#ESSEX-0001 … 8441, already sent)
    - master_ESSEX_raw_batch2.csv   batch 2  (#ESSEX-8442 … 20572, this run)
  The app's Stage 4 only ever loads master_<RC>_raw.csv, so batch-2 results
  would orphan against the batch-1 master and the guardrail would reject them.
  This script points the *exact same* join logic at the batch-2 master and
  writes master_ESSEX_enriched_batch2.csv, leaving batch 1 untouched.

Differences from stages.apollo_ingest.ingest_multiple:
  - Targets the batch-2 master filename.
  - Blank-fill only: a value is written ONLY into an empty master cell, so the
    failed/partial 10k file can never clobber good data from the two good files
    (first non-blank value wins). The stock function overwrites on any non-blank.
  - Per-file report: rows, UID vs name matches, orphans, and cells filled — so a
    dud file is obvious at a glance.

Usage (run from the app/ dir):
  python ingest_batch2.py <file1.csv> <file2.csv> <file3.csv>
"""
import os
import sys

import pandas as pd

from stages.apollo_ingest import (
    APOLLO_RAW_COLS,
    APOLLO_INTERNAL_COLS,
    _name_key,
    _build_name_index,
    _identity_matches,
    _uid_surname_conflict,
    UID_CONFLICT_REFUSE_THRESHOLD,
    _fan_out_person_enrichment,
)

PROJECT_DIR = os.path.join(os.path.dirname(__file__), "projects", "6_ESSEX")
RAW = os.path.join(PROJECT_DIR, "master_ESSEX_raw_batch2.csv")
ENRICHED = os.path.join(PROJECT_DIR, "master_ESSEX_enriched_batch2.csv")


def load_master():
    src = ENRICHED if os.path.exists(ENRICHED) else RAW
    master = pd.read_csv(src, dtype=str, keep_default_na=False)
    print(f"Loaded master: {len(master):,} rows from {os.path.basename(src)}")
    for col in APOLLO_INTERNAL_COLS:
        if col not in master.columns:
            master[col] = ""
    return master


def ingest(file_paths):
    master = load_master()
    uid_to_idx = {uid: i for i, uid in enumerate(master["Unique ID"])}
    name_to_idx = _build_name_index(master)

    grand = {"rows": 0, "uid": 0, "name": 0, "orphan": 0, "filled_cells": 0}
    orphans = []
    bad_files = []

    for path in file_paths:
        try:
            apollo = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception as exc:
            print(f"  SKIP {os.path.basename(path)}: {exc}")
            continue

        # On a header collision Apollo's enriched copy is suffixed ".1" by pandas;
        # read enrichment from that copy so we don't pull the preserved upload
        # value into the Apollo field (matching below still uses preserved cols).
        apollo_src = {c: (f"{c}.1" if f"{c}.1" in apollo.columns else c)
                      for c in APOLLO_RAW_COLS
                      if c in apollo.columns or f"{c}.1" in apollo.columns}

        # Pre-flight: refuse a file produced against a renumbered master.
        checked, mism = _uid_surname_conflict(master, apollo)
        if checked and mism / checked > UID_CONFLICT_REFUSE_THRESHOLD:
            raise SystemExit(
                f"\nREFUSING {os.path.basename(path)}: {mism:,}/{checked:,} rows "
                f"carry a Unique ID whose Surname disagrees with the batch-2 master "
                f"— it was generated against a renumbered master and would mis-join."
            )

        f_uid = f_name = f_mismatch = f_orphan = f_filled = 0
        for _, row in apollo.iterrows():
            idx = None
            uid = str(row.get("Unique ID", "")).strip()
            surname = str(row.get("Surname", "")).strip()
            first = str(row.get("First Name", "")).strip()
            company = str(row.get("Company Name", "")).strip()
            if uid and uid in uid_to_idx:
                cand = uid_to_idx[uid]
                if _identity_matches(master, cand, surname, first):
                    idx = cand
                    f_uid += 1
                else:
                    f_mismatch += 1
            if idx is None and surname and first and company:
                key = _name_key(surname, first, company)
                if key in name_to_idx:
                    idx = name_to_idx[key]
                    f_name += 1
            if idx is None:
                orphans.append(row.to_dict())
                f_orphan += 1
                continue

            for raw, internal in zip(APOLLO_RAW_COLS, APOLLO_INTERNAL_COLS):
                src = apollo_src.get(raw)
                if not src:
                    continue
                val = str(row.get(src, "")).strip()
                # Blank-fill only: never overwrite an existing value.
                if val and not str(master.at[idx, internal]).strip():
                    master.at[idx, internal] = val
                    f_filled += 1

        matched = f_uid + f_name
        print(f"\n  {os.path.basename(path)}: {len(apollo):,} rows")
        print(f"    matched {matched:,}  ({f_uid:,} UID, {f_name:,} name) | "
              f"UID→wrong-person rejected {f_mismatch:,} | "
              f"orphan {f_orphan:,} | cells filled {f_filled:,}")
        if len(apollo) > 0 and matched == 0:
            bad_files.append((os.path.basename(path), len(apollo)))

        grand["rows"] += len(apollo)
        grand["uid"] += f_uid
        grand["name"] += f_name
        grand["orphan"] += f_orphan
        grand["filled_cells"] += f_filled

    if bad_files:
        details = "; ".join(f"{n} ({r:,} rows, 0 matched)" for n, r in bad_files)
        raise SystemExit(
            f"\nREFUSING: these files matched nothing in the batch-2 master "
            f"(not by UID, not by name): {details}. First batch-2 UID is "
            f"{master['Unique ID'].iloc[0]}. Wrong project/batch?"
        )

    fanned = _fan_out_person_enrichment(master, print)

    if orphans:
        op = os.path.join(PROJECT_DIR, "apollo_orphans_batch2.csv")
        pd.DataFrame(orphans).to_csv(op, index=False)
        print(f"\n  {grand['orphan']:,} orphan rows → {os.path.basename(op)}")

    master.to_csv(ENRICHED, index=False)

    # Coverage: how many master rows now carry an Apollo email (the headline
    # 'did we actually enrich this person' signal).
    email_col = "Apollo Email"
    enriched_people = 0
    if email_col in master.columns:
        enriched_people = int((master[email_col].str.strip() != "").sum())

    print("\n" + "=" * 60)
    print(f"DONE → {os.path.basename(ENRICHED)}  ({len(master):,} master rows)")
    print(f"  total apollo rows read : {grand['rows']:,}")
    print(f"  matched                : {grand['uid'] + grand['name']:,} "
          f"({grand['uid']:,} UID, {grand['name']:,} name)")
    print(f"  orphan                 : {grand['orphan']:,}")
    print(f"  cells filled           : {grand['filled_cells']:,}")
    print(f"  fanned-out duplicates  : {fanned:,}")
    print(f"  master rows with email : {enriched_people:,} / {len(master):,} "
          f"({enriched_people / max(len(master), 1):.0%})")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python ingest_batch2.py <file1> [file2] [file3]")
    ingest(sys.argv[1:])
