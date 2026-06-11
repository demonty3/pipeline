#!/usr/bin/env python3
"""
One-off: run the deterministic evidence pass over Essex's remaining Tentatives
(2026-06-11, after the Gemini layer was removed).

Contract: the deliverable's Y&T tab is FROZEN at 4,747 rows. The evidence pass
only ever upgrades T -> Y, and both labels live in the Y&T tab, so the total
cannot move — only the Yes/Tentative split improves. This script asserts that
before saving anything.

Usage (from app/):  python evidence_pass_essex.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as db
from stages.classifier import _evidence_pass2, _apollo_name, apply_decisions_and_save
from stages.exporter import build_export

PID = 6
RC = "ESSEX"
PDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", "6_ESSEX")
FROZEN_YT = 4747


def main():
    classified = os.path.join(PDIR, f"master_{RC}_classified.csv")
    master = pd.read_csv(classified, dtype=str, keep_default_na=False)
    before = master["Result"].value_counts().to_dict()
    print(f"before: {before}")
    assert before.get("Y", 0) + before.get("T", 0) == FROZEN_YT, \
        f"pre-condition failed: Y+T != {FROZEN_YT}"

    tentative = master[master["Result"] == "T"]
    rows = []
    for _, r in tentative.iterrows():
        rows.append({
            "unique_id": r["Unique ID"],
            "officer_name": str(r.get("Officer name", "")),
            "apollo_name": _apollo_name(r),
            "company_name": str(r.get("Company Name", "")),
            "surname": str(r.get("Surname", "")).strip(),
            "first_name": str(r.get("First Name", "")).strip(),
            "apollo_email": str(r.get("Apollo Email", "")).strip(),
        })
    print(f"running evidence pass over {len(rows):,} Tentative row(s)")

    log_path = os.path.join(PDIR, "classifications_log.csv")
    pass2_y, pass2_review = _evidence_pass2(PID, rows, log_path, db, print)

    n = apply_decisions_and_save(PID, PDIR, RC, db)
    after = pd.read_csv(classified, dtype=str, keep_default_na=False)["Result"] \
        .value_counts().to_dict()
    print(f"after:  {after}")
    yt = after.get("Y", 0) + after.get("T", 0)
    assert yt == FROZEN_YT, f"FROZEN total violated: Y+T = {yt} != {FROZEN_YT}"

    path = build_export(PDIR, RC, progress_cb=lambda m: print("  ", m))
    db.update_stage5_status(PID, "complete")
    print(f"\nOK — Y&T held at {FROZEN_YT}: {after.get('Y',0):,} Yes + "
          f"{after.get('T',0):,} Tentative ({pass2_y:,} upgraded by evidence)")
    print(f"deliverable: {path}")


if __name__ == "__main__":
    main()
