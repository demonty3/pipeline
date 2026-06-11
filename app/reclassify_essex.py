#!/usr/bin/env python3
"""One-off: re-run Stage 5 (classify) for the Essex project (pid 6) after the
mis-join recovery. run_stage.py resolves region ESSEX to the wrong project (two
ESSEX rows exist), so we drive the classifier directly against pid 6 / 6_ESSEX.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import database as db
from stages.classifier import run_passes_1_and_2, apply_decisions_and_save

PID = 6
RC = "ESSEX"
PDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", "6_ESSEX")
KEY = os.environ.get("GEMINI_API_KEY", "")


def log(m):
    print(m, flush=True)


if __name__ == "__main__":
    if not KEY:
        raise SystemExit("GEMINI_API_KEY not set (app/.env)")
    run_passes_1_and_2(PID, PDIR, RC, KEY, progress_cb=log, db=db)
    n = apply_decisions_and_save(PID, PDIR, RC, db)
    print(f"OK classify — wrote master_{RC}_classified.csv ({n:,} rows)", flush=True)
