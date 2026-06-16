#!/usr/bin/env python3
"""
run_stage.py — headless driver for the 8-stage CCHQ pipeline.

This is the engine the cchq-orchestrator skill calls. It is a thin CLI
shell around the existing stage modules in ``app/stages/`` — it does NOT
re-implement any pipeline logic, it just lets Claude run one stage at a
time from the command line, file-in / file-out, without the Flask UI.

It reuses the app's own SQLite index (``app/projects.db``) and per-project
folder convention (``app/projects/<id>_<REGION>/``), so a project driven by
this script is the SAME project you'd see in the web app. You can drive a
run from the skill and still open it in Flask to eyeball it, or vice versa.

Usage
-----
    python run_stage.py --region LE1 <stage> [options]

Stages (run in order; each gates on the previous one's output file):
    init                      Create / fetch the project row for this region
    fetch    --area SW1A      Stage 1 — Companies House search for one postcode
    merge                     Stage 2 — regional merge + Unique IDs + SIC labels
    magazine [--budget N]     Stage 3 — build Apollo upload batches (<=10k each)
    ingest   --files a.csv... Stage 4 — re-stitch Apollo's enriched exports
    classify                  Stage 5 — Y/T/N classifier (fully deterministic)
    vs-export                 Stage 6a — build the VoteSource upload (Y/T rows)
    vs-return --file r.csv    Stage 6b — fold a returned VoteSource file back in
    re-flag  --file re.csv    Stage 7 — Raiser's Edge fuzzy flagger
    export                    Stage 8 — final multi-tab deliverable XLSX
    status                    Print every stage's status + which files exist

Environment (put these in app/.env or the real environment):
    CH_API_KEY        Companies House API key  (Stage 1)

Design notes
------------
- Every command prints stage progress to stdout (the stage modules' own
  ``progress_cb`` log lines) so Claude can read what happened and decide the
  next move. The last line of a successful run is ``OK <stage>``.
- Stage 5 is fully deterministic: fuzzy score -> evidence pass (nicknames +
  email corroboration) -> human review. No LLM anywhere in the pipeline.
  Any rows the cascade leaves in the human-review band are reported in the
  summary and written to the review queue; rerun ``status`` to see counts.
- Stage 7 is a single deterministic pass (no Gemini, no review queue) — RE
  donor data must never reach an LLM. Every hit gets a certainty tier
  (Match / Probable / Potential); see references/schema_contract.md.
"""
import argparse
import json
import os
import sys

# ── Make the app package importable ─────────────────────────────────────────
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(SKILL_DIR)
APP_DIR = os.path.join(PROJECT_ROOT, "app")
sys.path.insert(0, APP_DIR)

# Load app/.env if present (so CH_API_KEY is available).
_env_path = os.path.join(APP_DIR, ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import database as db  # noqa: E402
from stages.ch_fetch import fetch_postcode  # noqa: E402
from stages.merge import run_merge  # noqa: E402
from stages.apollo_magazine import build_batches  # noqa: E402
from stages.apollo_ingest import ingest_multiple  # noqa: E402
from stages.classifier import (  # noqa: E402
    run_passes_1_and_2, apply_decisions_and_save,
)
from stages.vs_export import build_vs_export  # noqa: E402
from stages.re_flagger import run_re_flagging, apply_re_decisions_and_save  # noqa: E402
from stages.exporter import build_export, _pick_master  # noqa: E402


def log(msg):
    print(msg, flush=True)


def get_or_create_project(region_code):
    """Return the project_id for this region, creating the row if needed."""
    region_code = region_code.upper()
    db.init_db()
    for p in db.get_all_projects():
        if p["region_code"].upper() == region_code:
            return p["id"]
    pid = db.create_project(
        name=f"{region_code} (skill run)",
        region_code=region_code,
        event_date=None,
        postcodes=[],
    )
    log(f"Created project #{pid} for region {region_code}")
    return pid


def require_env(key):
    val = os.environ.get(key, "")
    if not val:
        log(f"ERROR: {key} is not set. Add it to app/.env or the environment.")
        sys.exit(2)
    return val


# ── Stage handlers ──────────────────────────────────────────────────────────

def _warn_if_blank(path, column, label, threshold=0.20):
    """Warn loudly if a load-bearing column is largely blank. Catches a Stage-1
    pull that silently returned no company names (one SW1 demo area came back
    0/441) — Company Name drives Apollo name-matching and the deliverable, so a
    blank column quietly degrades downstream output with no other signal."""
    import pandas as pd
    if not os.path.exists(path):
        return
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if column not in df.columns or len(df) == 0:
        return
    blank = int((df[column].astype(str).str.strip() == "").sum())
    if blank and blank / len(df) > threshold:
        log(f"WARNING: {label}: {blank:,}/{len(df):,} rows ({blank/len(df)*100:.0f}%) "
            f"have a blank '{column}'. This degrades Apollo name-matching and the "
            f"deliverable — check the Companies House pull for this area.")


def stage_fetch(pid, pdir, region, args):
    api_key = require_env("CH_API_KEY")
    area = args.area or region
    out_dir = os.path.join(pdir, "postcodes")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"results_{area}.csv")
    n = fetch_postcode(area, api_key, out_path, progress_cb=log)
    _warn_if_blank(out_path, "Company Name", f"fetch {area}")
    db.update_stage1_status(pid, "complete")
    log(f"OK fetch — {area}: {n} rows -> {out_path}")


def stage_merge(pid, pdir, region, args):
    raw_path = os.path.join(pdir, f"master_{region}_raw.csv")
    if os.path.exists(raw_path) and not args.force:
        log(f"ERROR: {os.path.basename(raw_path)} already exists. Re-running merge "
            f"reassigns every Unique ID, which orphans the Stage 5/7 decisions keyed "
            f"to the old IDs and breaks the deliverable's join. Pass --force to rebuild "
            f"from scratch (Unique IDs restart at #{region}-0001). If you only added "
            f"new postcodes, use the app's Unique-ID backfill/repair instead.")
        sys.exit(2)
    # On a forced rebuild restart the counter, so the same input yields the same
    # IDs; otherwise continue from the project's last-used counter.
    start = 0 if args.force else (db.get_project(pid).get("unique_id_counter", 0) or 0)
    rows, new_counter = run_merge(
        pdir, region, id_prefix=args.id_prefix or region,
        starting_counter=start, progress_cb=log,
    )
    db.set_unique_id_counter(pid, new_counter)
    _warn_if_blank(raw_path, "Company Name", f"merge {region}")
    db.update_stage2_status(pid, "complete")
    log(f"OK merge — {rows} rows, Unique IDs through #{region}-{new_counter:04d}")


def stage_magazine(pid, pdir, region, args):
    batches = build_batches(
        pdir, region, credit_budget=args.budget, progress_cb=log,
    )
    db.update_stage3_status(pid, "complete")
    log(f"OK magazine — {len(batches)} batch file(s)")


def stage_ingest(pid, pdir, region, args):
    if not args.files:
        log("ERROR: --files is required (one or more Apollo enriched CSVs)")
        sys.exit(2)
    ingest_multiple(pdir, region, args.files, progress_cb=log)
    db.update_stage4_status(pid, "complete")
    log("OK ingest — Apollo columns merged into master_enriched")


def _write_audit(pdir, name, queue_rows, fields):
    """Dump the rows left in the review band to an audit CSV so the
    'no human in the loop' decision stays traceable (scope requires logging
    every decision). Returns the path, or None if there was nothing to write."""
    import csv
    if not queue_rows:
        return None
    path = os.path.join(pdir, name)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in queue_rows:
            w.writerow(r)
    return path


def stage_classify(pid, pdir, region, args):
    run_passes_1_and_2(pid, pdir, region, progress_cb=log, db=db)
    rows = apply_decisions_and_save(pid, pdir, region, db)
    # No human gate: apply_decisions_and_save already wrote a label for every
    # row (missing -> 'T'). The "review queue" is the no-evidence Tentative
    # band — we keep those as T and log them for audit.
    queue = db.get_s5_review_queue(pid)
    audit = _write_audit(pdir, f"stage5_autoresolved_{region}.csv", queue,
                         ["unique_id", "label", "confidence", "reason"])
    import pandas as pd
    cl = pd.read_csv(os.path.join(pdir, f"master_{region}_classified.csv"),
                     dtype=str, keep_default_na=False)
    counts = cl["Result"].value_counts().to_dict() if "Result" in cl else {}
    db.update_stage5_status(pid, "complete")
    log(f"OK classify — {rows} rows fully classified (no human gate). "
        f"Y/T/N = {counts.get('Y', 0)}/{counts.get('T', 0)}/{counts.get('N', 0)}; "
        f"{len(queue)} no-evidence rows left Tentative"
        + (f" -> audit: {os.path.basename(audit)}" if audit else ""))


def stage_vs_export(pid, pdir, region, args):
    path = build_vs_export(pdir, region, progress_cb=log)
    db.update_stage6_status(pid, "review")  # async: waiting on the return file
    log(f"OK vs-export — upload file ready: {path}")


def stage_vs_return(pid, pdir, region, args):
    import pandas as pd
    if not args.file:
        log("ERROR: --file is required (the returned VoteSource file)")
        sys.exit(2)
    f = args.file
    vs = (pd.read_csv(f, dtype=str, keep_default_na=False) if f.lower().endswith(".csv")
          else pd.read_excel(f, dtype=str, keep_default_na=False))
    if "UniqueID" in vs.columns and "Unique ID" not in vs.columns:
        vs = vs.rename(columns={"UniqueID": "Unique ID"})
    if "Unique ID" not in vs.columns:
        log("ERROR: VS return file needs a 'Unique ID' (or 'UniqueID') column")
        sys.exit(2)
    drop = [c for c in vs.columns if not c or (isinstance(c, str) and c.startswith("Unnamed:"))]
    vs = vs.drop(columns=drop) if drop else vs
    # A duplicated Unique ID in the return file would multiply master rows on the
    # left-merge below, breaking the "never re-order or filter rows" contract.
    dup_ct = int(vs["Unique ID"].duplicated().sum())
    if dup_ct:
        log(f"WARNING: VS return has {dup_ct} duplicate Unique ID row(s); "
            f"keeping the first of each")
        vs = vs.drop_duplicates(subset="Unique ID", keep="first")
    classified = os.path.join(pdir, f"master_{region}_classified.csv")
    if not os.path.exists(classified):
        log(f"ERROR: {os.path.basename(classified)} not found — run Stage 5 (classify) first.")
        sys.exit(2)
    master = pd.read_csv(classified, dtype=str, keep_default_na=False)
    new_cols = [c for c in vs.columns if c != "Unique ID" and c not in master.columns]
    master = master.merge(vs[["Unique ID"] + new_cols], on="Unique ID", how="left")
    master[new_cols] = master[new_cols].fillna("")
    out = os.path.join(pdir, f"master_{region}_vs.csv")
    master.to_csv(out, index=False)
    db.update_stage6_status(pid, "complete")
    log(f"OK vs-return — {len(new_cols)} VS column(s) folded in -> {out}")


def stage_re_flag(pid, pdir, region, args):
    # Deterministic tiering only — RE donor data is highly sensitive and must
    # never reach an LLM (Charles, 2026-06-10). No API key, no human gate:
    # every hit lands on the Potential RE Match tab with its certainty tier
    # (Match / Probable / Potential) so we err toward over-flagging.
    if not args.file:
        log("ERROR: --file is required (the Raiser's Edge export)")
        sys.exit(2)
    summary = run_re_flagging(pid, pdir, region, args.file, progress_cb=log, db=db)
    apply_re_decisions_and_save(pid, pdir, region, db)
    db.update_stage7_status(pid, "complete")
    log(f"OK re-flag — {json.dumps(summary)}")


def stage_export(pid, pdir, region, args):
    path = build_export(pdir, region, progress_cb=log)
    db.update_stage8_status(pid, "complete")
    log(f"OK export — deliverable: {path}")


def stage_summary(pid, pdir, region, args):
    """Build the deliverable/status email text for this region and write it to
    the skill's state/ folder. The orchestrator picks this up and calls the
    Gmail create_draft tool once compose scope is granted; until then the text
    sits locally so nothing is lost. Pure read — never mutates the pipeline."""
    import pandas as pd
    try:
        master_path, _ = _pick_master(pdir, region)
    except FileNotFoundError:
        log("ERROR: no master file — run at least Stage 2 first.")
        sys.exit(2)
    m = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    total = len(m)
    res = m["Result"].value_counts().to_dict() if "Result" in m else {}
    yt = res.get("Y", 0) + res.get("T", 0)
    apollo_hits = int((m.get("Apollo Email", pd.Series([""] * total)).astype(str).str.strip() != "").sum())
    re_hits = int((m.get("RE Match?", pd.Series([""] * total)).astype(str).str.strip().str.upper() == "Y").sum())
    hit_rate = f"{apollo_hits / total * 100:.0f}%" if total else "n/a"
    deliverable = os.path.join(pdir, f"{region}_final_deliverable.xlsx")
    has_deliverable = os.path.exists(deliverable)

    subject = f"CCHQ {region} list — {yt:,} contactable prospects ready"
    body = (
        f"Hi,\n\n"
        f"The {region} donor-outreach list is ready.\n\n"
        f"  - Total master rows: {total:,}\n"
        f"  - Apollo matched: {apollo_hits:,} ({hit_rate} hit rate)\n"
        f"  - Y/T/N split: {res.get('Y', 0):,} Yes / {res.get('T', 0):,} Tentative / {res.get('N', 0):,} No\n"
        f"  - Y&T (contactable) tab: {yt:,} rows\n"
        f"  - Potential RE matches flagged: {re_hits:,}\n\n"
        f"Deliverable: {os.path.basename(deliverable) if has_deliverable else '(run export to generate)'}\n"
        f"Source master: {os.path.basename(master_path)}\n\n"
        f"Stages 5 & 7 ran autonomously; every classifier/flagger decision is "
        f"logged in classifications_log.csv for audit.\n\n"
        f"Best,\nCCHQ pipeline (automated)\n"
    )
    state_dir = os.path.join(SKILL_DIR, "state")
    os.makedirs(state_dir, exist_ok=True)
    draft_path = os.path.join(state_dir, f"draft_{region}.md")
    with open(draft_path, "w") as fh:
        fh.write(f"To: c.ames@cloudmundi.com\nSubject: {subject}\n\n{body}")
    log(f"Subject: {subject}")
    log(body)
    log(f"OK summary — draft written to {draft_path}"
        + ("" if has_deliverable else " (NOTE: no deliverable yet — run export)"))


def stage_status(pid, pdir, region, args):
    p = db.get_project(pid)
    log(f"Project #{pid}  region={region}  dir={pdir}")
    for n in range(1, 9):
        log(f"  Stage {n}: {p.get(f'stage{n}_status', '?')}")
    log("  Files present:")
    for name in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if name.endswith((".csv", ".xlsx")):
            log(f"    - {name}")
    log("OK status")


HANDLERS = {
    "fetch": stage_fetch, "merge": stage_merge, "magazine": stage_magazine,
    "ingest": stage_ingest, "classify": stage_classify,
    "vs-export": stage_vs_export, "vs-return": stage_vs_return,
    "re-flag": stage_re_flag, "export": stage_export,
    "summary": stage_summary, "status": stage_status,
}


def main():
    ap = argparse.ArgumentParser(description="Headless CCHQ 8-stage pipeline driver")
    ap.add_argument("--region", required=True, help="Region code, e.g. LE1")
    ap.add_argument("stage", choices=["init"] + list(HANDLERS.keys()))
    ap.add_argument("--area", help="Postcode area for `fetch` (defaults to region)")
    ap.add_argument("--id-prefix", help="Unique ID prefix for `merge` (defaults to region)")
    ap.add_argument("--budget", type=int, help="Apollo credit budget cap for `magazine`")
    ap.add_argument("--files", nargs="+", help="Apollo enriched CSV(s) for `ingest`")
    ap.add_argument("--file", help="Single input file for `vs-return` / `re-flag`")
    ap.add_argument("--force", action="store_true",
                    help="For `merge`: rebuild from scratch even if a master "
                         "already exists (Unique IDs restart at 0001).")
    args = ap.parse_args()

    region = args.region.upper()
    pid = get_or_create_project(region)
    pdir = db.project_dir(pid, region)
    os.makedirs(pdir, exist_ok=True)

    if args.stage == "init":
        log(f"OK init — project #{pid} ready at {pdir}")
        return
    HANDLERS[args.stage](pid, pdir, region, args)


if __name__ == "__main__":
    main()
