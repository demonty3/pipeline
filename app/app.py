"""
CCHQ Business Club Building — Project Orchestrator
All 8 stages.
"""
import glob
import io
import os
import re
import shutil
import threading
import tempfile
import pandas as pd
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, send_file, abort)
from dotenv import load_dotenv

import database as db
from stages.ch_fetch import fetch_postcode, count_area
from stages.merge import run_merge, backfill_unique_ids, count_missing_unique_ids
from stages.apollo_magazine import build_batches
from stages.apollo_ingest import ingest_batch, ingest_multiple
from stages.classifier import (run_passes_1_and_2, apply_decisions_and_save,
                                retry_gemini_failed_rows as classifier_retry,
                                count_apollo_placeholders_in_queue,
                                reclassify_apollo_placeholders)
from stages.re_flagger import (run_re_flagging, apply_re_decisions_and_save,
                                retry_gemini_failed_rows as re_flagger_retry)
from stages import gemini_health
from stages.exporter import build_export
from stages.vs_export import build_vs_export

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-key-change-me")

CH_API_KEY     = os.getenv("CH_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── In-memory job state ───────────────────────────────────────────────────────
_job_state: dict = {}
_job_lock = threading.Lock()

# Per-project cancel events — set to signal a running background thread to stop.
_cancel_events: dict = {}  # project_id → threading.Event

def _job(pid):
    with _job_lock:
        return _job_state.get(pid)

def _set_job(pid, state):
    with _job_lock:
        _job_state[pid] = state

def _update_pc(pid, pc, **kwargs):
    with _job_lock:
        _job_state[pid]["postcodes"][pc].update(kwargs)

def _update_job(pid, **kwargs):
    with _job_lock:
        if pid in _job_state:
            _job_state[pid].update(kwargs)


# ── DB init ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _existing_area_csvs(pdir, areas):
    """Return dict of area → bool (whether results CSV exists for that area)."""
    result = {}
    for area in areas:
        fname = f"results_{area.replace(' ', '').upper()}.csv"
        result[area] = os.path.exists(os.path.join(pdir, "postcodes", fname))
    return result

def _stage_gate(project, required_stage, required_status="complete"):
    """Return an error JSON if the prerequisite stage is not done."""
    col = f"stage{required_stage}_status"
    val = project.get(col, "pending")
    if required_status == "complete":
        ok = val in ("complete", "skipped")
    else:
        ok = val == required_status
    if not ok:
        return jsonify({"error": f"Stage {required_stage} must be {required_status} first (currently: {val})"}), 400
    return None

def _safe_path(pdir, filename):
    """Reject path traversal attempts."""
    full = os.path.abspath(os.path.join(pdir, filename))
    if not full.startswith(os.path.abspath(pdir)):
        abort(403)
    return full


# ═══════════════════════════════════════════════════════════════════════════════
# Project list + create
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    projects = db.get_all_projects()
    return render_template("index.html", projects=projects)


@app.route("/project/new", methods=["GET", "POST"])
def new_project():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        region_code = request.form.get("region_code", "").strip().upper()
        event_date = request.form.get("event_date", "").strip()

        errors = []
        if not name:        errors.append("Project name is required.")
        if not region_code: errors.append("Region/ID prefix is required (e.g. LE1).")

        if errors:
            return render_template("new_project.html", errors=errors, name=name,
                                   region_code=region_code, event_date=event_date)

        project_id = db.create_project(name, region_code, event_date, [])
        pdir = db.project_dir(project_id, region_code)
        os.makedirs(os.path.join(pdir, "postcodes"), exist_ok=True)
        return redirect(url_for("project_detail", project_id=project_id))

    return render_template("new_project.html", errors=[], name="", region_code="",
                           event_date="")


# ═══════════════════════════════════════════════════════════════════════════════
# Project detail
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>")
def project_detail(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    existing_csvs = _existing_area_csvs(pdir, project["postcodes"])
    batches = db.get_apollo_batches(project_id)
    ingested = sum(1 for b in batches if b["status"] == "ingested")

    files = {
        "master_raw":     os.path.exists(os.path.join(pdir, f"master_{rc}_raw.csv")),
        "master_enriched":os.path.exists(os.path.join(pdir, f"master_{rc}_enriched.csv")),
        "master_classified": os.path.exists(os.path.join(pdir, f"master_{rc}_classified.csv")),
        "master_vs":      os.path.exists(os.path.join(pdir, f"master_{rc}_vs.csv")),
        "master_re":      os.path.exists(os.path.join(pdir, f"master_{rc}_re_flagged.csv")),
        "final":          os.path.exists(os.path.join(pdir, f"{rc}_final_deliverable.xlsx")),
        "class_log":      os.path.exists(os.path.join(pdir, "classifications_log.csv")),
        "vs_export":      os.path.exists(os.path.join(pdir, f"vs_export_{rc}.csv")),
    }

    return render_template(
        "project.html",
        project=project,
        logs1=db.get_logs(project_id, stage=1),
        logs2=db.get_logs(project_id, stage=2),
        logs3=db.get_logs(project_id, stage=3),
        logs4=db.get_logs(project_id, stage=4),
        logs5=db.get_logs(project_id, stage=5),
        logs6=db.get_logs(project_id, stage=6),
        logs7=db.get_logs(project_id, stage=7),
        logs8=db.get_logs(project_id, stage=8),
        job=_job(project_id),
        existing_csvs=existing_csvs,
        batches=batches,
        batches_ingested=ingested,
        files=files,
        has_api_key=bool(CH_API_KEY),
        has_gemini=bool(GEMINI_API_KEY),
        s5_review_count=len(db.get_s5_review_queue(project_id)),
        s7_review_count=len(db.get_s7_review_queue(project_id)),
        missing_uids=count_missing_unique_ids(pdir, rc),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Status polling (used by all stage progress bars)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/status.json")
def job_status(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    return jsonify({
        "stage1_status": project["stage1_status"],
        "stage2_status": project["stage2_status"],
        "stage3_status": project["stage3_status"],
        "stage4_status": project["stage4_status"],
        "stage5_status": project["stage5_status"],
        "stage6_status": project["stage6_status"],
        "stage7_status": project["stage7_status"],
        "stage8_status": project["stage8_status"],
        "job": _job(project_id),
        "master_raw_exists": os.path.exists(os.path.join(pdir, f"master_{rc}_raw.csv")),
        "final_exists": os.path.exists(os.path.join(pdir, f"{rc}_final_deliverable.xlsx")),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Pause + Delete
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/pause", methods=["POST"])
def pause_job(project_id):
    """Signal any running background thread for this project to stop after its current unit."""
    ev = _cancel_events.get(project_id)
    if ev:
        ev.set()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/delete", methods=["POST"])
def delete_project(project_id):
    """Cancel any running job, delete project folder, remove from DB, redirect to index."""
    ev = _cancel_events.get(project_id)
    if ev:
        ev.set()

    project = db.get_project(project_id)
    if project:
        pdir = db.project_dir(project_id, project["region_code"])
        if os.path.exists(pdir):
            shutil.rmtree(pdir)

    db.delete_project(project_id)
    return redirect(url_for("index"))


# ═══════════════════════════════════════════════════════════════════════════════
# File download
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/download/<path:filename>")
def download_file(project_id, filename):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    pdir = db.project_dir(project_id, project["region_code"])
    full_path = _safe_path(pdir, filename)
    if not os.path.exists(full_path):
        abort(404)
    return send_file(full_path, as_attachment=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1 — Companies House fetch
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/project/<int:project_id>/fetch/count", methods=["POST"])
def fetch_count(project_id):
    """Return active company counts per area — used by the preview button."""
    if not CH_API_KEY:
        return jsonify({"error": "CH_API_KEY not set in .env"}), 400
    raw = request.form.get("search_areas", "")
    areas = [a.strip().upper() for a in raw.splitlines() if a.strip()]
    if not areas:
        return jsonify({"error": "No areas provided"}), 400
    results = [count_area(a, CH_API_KEY) for a in areas]
    return jsonify({"counts": results})


@app.route("/project/<int:project_id>/stage1/preview")
def stage1_preview(project_id):
    """Return a sample of Stage 1 results for sanity-checking."""
    project = db.get_project(project_id)
    if not project:
        abort(404)
    pdir = db.project_dir(project_id, project["region_code"])
    csvs = sorted(glob.glob(os.path.join(pdir, "postcodes", "results_*.csv")))
    if not csvs:
        return jsonify({"rows": [], "total": 0})
    frames = []
    for f in csvs[:5]:
        try:
            frames.append(pd.read_csv(f, dtype=str, keep_default_na=False, nrows=20))
        except Exception:
            pass
    if not frames:
        return jsonify({"rows": [], "total": 0})
    sample = pd.concat(frames, ignore_index=True).head(20)
    cols = ["Officer name", "Officer role", "Officer occupation",
            "Company Name", "Company address post code", "Officer date of birth"]
    available = [c for c in cols if c in sample.columns]
    return jsonify({"rows": sample[available].to_dict("records"), "total": len(sample)})


@app.route("/project/<int:project_id>/fetch", methods=["POST"])
def start_fetch(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    if not CH_API_KEY:
        return jsonify({"error": "CH_API_KEY not set in .env"}), 400
    job = _job(project_id)
    if job and job.get("stage") == 1 and job.get("status") == "running":
        return jsonify({"error": "Fetch already running"}), 409

    # Read search areas from the form and persist them on the project
    raw_areas = request.form.get("search_areas", "")
    areas = [a.strip().upper() for a in raw_areas.splitlines() if a.strip()]
    if not areas:
        areas = project["postcodes"]  # fall back to whatever was stored
    if not areas:
        return jsonify({"error": "Enter at least one search area (postcode district or town)"}), 400

    db.update_search_areas(project_id, areas)
    project["postcodes"] = areas

    _set_job(project_id, {
        "stage": 1, "status": "running",
        "postcodes": {a: {"status": "pending", "rows": 0, "log": []} for a in areas},
        "error": None,
    })
    db.update_stage1_status(project_id, "running")
    ev = threading.Event()
    _cancel_events[project_id] = ev
    threading.Thread(target=_run_fetch, args=(project_id, project, ev), daemon=True).start()
    return jsonify({"ok": True})


def _run_fetch(project_id, project, cancel_event):
    areas = project["postcodes"]
    region_code = project["region_code"]
    pdir = db.project_dir(project_id, region_code)
    total_rows = 0
    error_pcs = []
    cancelled = False

    for pc in areas:
        if cancel_event.is_set():
            cancelled = True
            break

        _update_pc(project_id, pc, status="running")
        output_path = os.path.join(pdir, "postcodes", f"results_{pc.replace(' ', '')}.csv")
        pc_log = []

        def cb(msg, _pc=pc, _log=pc_log):
            _log.append(msg)
            _update_pc(project_id, _pc, log=list(_log))
            db.add_log(project_id, 1, f"[{_pc}] {msg}")

        try:
            rows = fetch_postcode(pc, CH_API_KEY, output_path, progress_cb=cb)
            _update_pc(project_id, pc, status="done", rows=rows)
            total_rows += rows
        except Exception as exc:
            _update_pc(project_id, pc, status="error", log=pc_log + [f"ERROR: {exc}"])
            db.add_log(project_id, 1, f"[{pc}] ERROR: {exc}")
            error_pcs.append(pc)

    _cancel_events.pop(project_id, None)

    if cancelled:
        db.update_stage1_status(project_id, "paused")
        db.add_log(project_id, 1, f"Fetch paused — {total_rows:,} rows saved so far")
        _update_job(project_id, status="paused")
    elif error_pcs:
        db.update_stage1_status(project_id, "error")
        _update_job(project_id, status="error", error=f"Errors in: {', '.join(error_pcs)}")
    else:
        db.update_stage1_status(project_id, "complete")
        db.add_log(project_id, 1, f"Stage 1 complete — {total_rows:,} rows")
        _update_job(project_id, status="done")


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2 — Regional merge
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/merge", methods=["POST"])
def start_merge(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    if project["stage1_status"] != "complete":
        return jsonify({"error": "Stage 1 must be complete before merging"}), 400

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    db.update_stage2_status(project_id, "running")
    try:
        row_count, new_counter = run_merge(
            project_dir=pdir, region_code=rc, id_prefix=rc,
            starting_counter=project["unique_id_counter"],
            progress_cb=lambda m: db.add_log(project_id, 2, m),
        )
        db.set_unique_id_counter(project_id, new_counter)
        db.update_stage2_status(project_id, "complete")
        db.add_log(project_id, 2, f"Stage 2 complete — {row_count:,} rows")
    except Exception as exc:
        db.update_stage2_status(project_id, "error")
        db.add_log(project_id, 2, f"ERROR: {exc}")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "rows": row_count})


# ── Repair: backfill missing Unique IDs ────────────────────────────────────────

@app.route("/project/<int:project_id>/repair/unique-ids", methods=["POST"])
def repair_unique_ids(project_id):
    """
    Backfill empty Unique IDs across every master_<RC>_*.csv in this project,
    then (if a final XLSX exists) regenerate it so the deliverable picks up
    the new IDs. Fixes projects where import_master accepted a file with an
    empty Unique ID column under the older validation.
    """
    project = db.get_project(project_id)
    if not project:
        abort(404)
    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    def cb(msg):
        db.add_log(project_id, 2, msg)

    n_updated, new_counter = backfill_unique_ids(pdir, rc, project["unique_id_counter"], progress_cb=cb)
    db.set_unique_id_counter(project_id, new_counter)
    db.add_log(project_id, 2,
               f"Repair: backfilled Unique IDs across {n_updated} master file(s); counter → {new_counter}")

    # If a final XLSX exists, regenerate it so the Treasurers get the fixed file.
    final_path = os.path.join(pdir, f"{rc}_final_deliverable.xlsx")
    if os.path.exists(final_path):
        build_export(pdir, rc, progress_cb=lambda m: db.add_log(project_id, 8, m))
        db.add_log(project_id, 8, "Regenerated final deliverable with backfilled Unique IDs")

    return redirect(url_for("project_detail", project_id=project_id))


# ── Import master shortcut ─────────────────────────────────────────────────────

@app.route("/project/<int:project_id>/import-master", methods=["POST"])
def import_master(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    fname = f.filename.lower()
    pdir = db.project_dir(project_id, project["region_code"])
    os.makedirs(pdir, exist_ok=True)

    try:
        if fname.endswith(".csv"):
            master = pd.read_csv(f, dtype=str, keep_default_na=False)
        elif fname.endswith((".xlsx", ".xls")):
            master = pd.read_excel(f, dtype=str, keep_default_na=False)
        else:
            return jsonify({"error": "Only CSV or XLSX files accepted"}), 400
    except Exception as exc:
        return jsonify({"error": f"Could not read file: {exc}"}), 400

    rc = project["region_code"]

    # Backfill missing Unique IDs at upload time. The previous validation only
    # checked the column existed — Apollo bulk-CSV exports can come back with
    # the header present but every value empty, which silently propagated
    # blanks all the way to the Treasurers' final deliverable.
    if "Unique ID" not in master.columns:
        master.insert(0, "Unique ID", "")

    def _extract_num(uid):
        m = re.search(r"(\d+)$", str(uid))
        return int(m.group(1)) if m else 0

    # Seed the counter from the project AND any existing IDs in the file —
    # whichever is higher — so re-imports never collide with previously-
    # assigned IDs.
    counter = max(project["unique_id_counter"],
                  int(master["Unique ID"].apply(_extract_num).max() or 0))
    mask_empty = master["Unique ID"].astype(str).str.strip() == ""
    n_missing = int(mask_empty.sum())
    if n_missing > 0:
        new_ids = []
        for _ in range(n_missing):
            counter += 1
            new_ids.append(f"#{rc}-{counter:04d}")
        master.loc[mask_empty, "Unique ID"] = new_ids

    out_path = os.path.join(pdir, f"master_{rc}_raw.csv")
    master.to_csv(out_path, index=False)

    db.set_unique_id_counter(project_id, counter)

    db.update_stage1_status(project_id, "complete")
    db.update_stage2_status(project_id, "complete")
    detail = f"{len(master):,} rows, max UID counter: {counter}"
    if n_missing > 0:
        detail += f" (backfilled {n_missing:,} missing IDs)"
    db.add_log(project_id, 2, f"Imported master — {detail}")

    return redirect(url_for("project_detail", project_id=project_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3 — Apollo magazine
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/stage3/build", methods=["POST"])
def stage3_build(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    err = _stage_gate(project, 2)
    if err:
        return err

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    exclude_dissolved = request.form.get("exclude_dissolved") == "on"
    exclude_non_uk = request.form.get("exclude_non_uk") == "on"
    # Optional credit budget. Operator types it in, or pre-fills via /apollo/credits.
    # Empty or zero means "no cap".
    credit_budget = request.form.get("credit_budget", type=int)
    if credit_budget is not None and credit_budget <= 0:
        credit_budget = None
    batch_size = request.form.get("batch_size", type=int)
    if batch_size is not None and batch_size <= 0:
        batch_size = None

    db.update_stage3_status(project_id, "running")
    db.delete_apollo_batches(project_id)

    try:
        def cb(msg):
            db.add_log(project_id, 3, msg)

        batches = build_batches(pdir, rc, exclude_dissolved=exclude_dissolved,
                                exclude_non_uk=exclude_non_uk,
                                credit_budget=credit_budget,
                                batch_size=batch_size, progress_cb=cb)
        for b in batches:
            db.create_apollo_batch(project_id, b["batch_num"], b["row_count"], b["filename"])

        db.update_stage3_status(project_id, "complete")
        db.add_log(project_id, 3, f"Stage 3 complete — {len(batches)} batch(es)")
    except Exception as exc:
        db.update_stage3_status(project_id, "error")
        db.add_log(project_id, 3, f"ERROR: {exc}")
        return jsonify({"error": str(exc)}), 500

    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/apollo/credits", methods=["GET"])
def apollo_credits_route():
    """
    Best-effort fetch of Apollo credits remaining. Returns
    {"credits_remaining": N} on success, or {"credits_remaining": null,
    "reason": "..."} if we couldn't determine it. The Stage 3 form's
    "Fetch from Apollo" button hits this to pre-fill the credit-budget input.
    """
    from stages.apollo_credits import fetch_credits_remaining
    credits = fetch_credits_remaining()
    if credits is None:
        reason = ("APOLLO_API_KEY not set in .env"
                  if not os.getenv("APOLLO_API_KEY")
                  else "Apollo did not return a recognised credits field")
        return jsonify({"credits_remaining": None, "reason": reason})
    return jsonify({"credits_remaining": credits})


@app.route("/project/<int:project_id>/stage3/mark-sent", methods=["POST"])
def stage3_mark_sent(project_id):
    batch_id = request.form.get("batch_id", type=int)
    if not batch_id:
        abort(400)
    db.update_batch_status(batch_id, "sent")
    return redirect(url_for("project_detail", project_id=project_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 4 — Apollo result ingestion
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/stage4/upload-result", methods=["POST"])
def stage4_upload(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    err = _stage_gate(project, 3)
    if err:
        return err

    batch_id = request.form.get("batch_id", type=int)
    if not batch_id:
        return jsonify({"error": "batch_id required"}), 400
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    # Save to a temp file so ingest_batch can read it
    suffix = ".csv" if f.filename.lower().endswith(".csv") else ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        db.update_batch_status(batch_id, "results_in")
        result = ingest_batch(
            pdir, rc, tmp_path, batch_id,
            progress_cb=lambda m: db.add_log(project_id, 4, m),
        )
        db.update_batch_status(batch_id, "ingested")
        db.add_log(project_id, 4, f"Batch {batch_id} ingested: {result['matched']:,} matched, {result['unmatched']:,} orphans")
    except Exception as exc:
        db.update_batch_status(batch_id, "results_in")  # revert to allow re-upload
        db.add_log(project_id, 4, f"ERROR batch {batch_id}: {exc}")
        return jsonify({"error": str(exc)}), 500
    finally:
        os.unlink(tmp_path)

    # Check if all batches are ingested
    batches = db.get_apollo_batches(project_id)
    if batches and all(b["status"] == "ingested" for b in batches):
        db.update_stage4_status(project_id, "complete")
        db.add_log(project_id, 4, "Stage 4 complete — all batches ingested")
    else:
        db.update_stage4_status(project_id, "running")

    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage4/upload-multiple", methods=["POST"])
def stage4_upload_multiple(project_id):
    """Accept multiple Apollo result CSVs and ingest all at once by Unique ID."""
    project = db.get_project(project_id)
    if not project:
        abort(404)
    files = request.files.getlist("files")
    if not files or all(not f.filename for f in files):
        return jsonify({"error": "No files uploaded"}), 400

    pdir = db.project_dir(project_id, project["region_code"])
    tmp_paths = []
    try:
        for f in files:
            if not f.filename:
                continue
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                f.save(tmp.name)
                tmp_paths.append(tmp.name)

        result = ingest_multiple(
            pdir, project["region_code"], tmp_paths,
            progress_cb=lambda m: db.add_log(project_id, 4, m),
        )
        # Mark all sent batches as ingested — we matched by ID so batch order is irrelevant
        for b in db.get_apollo_batches(project_id):
            if b["status"] in ("sent", "results_in"):
                db.update_batch_status(b["id"], "ingested")
        db.update_stage4_status(project_id, "complete")
        db.add_log(project_id, 4,
            f"Multi-ingest: {result['matched']:,} matched from {result['files_processed']} file(s)")
    except Exception as exc:
        db.add_log(project_id, 4, f"ERROR: {exc}")
        return jsonify({"error": str(exc)}), 500
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage4/prefill", methods=["POST"])
def stage4_prefill(project_id):
    """Copy Apollo enrichment columns from a previous enriched master CSV by name+DOB match."""
    project = db.get_project(project_id)
    if not project:
        abort(404)
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    master_path = os.path.join(pdir, f"master_{rc}_raw.csv")
    enriched_path = os.path.join(pdir, f"master_{rc}_enriched.csv")

    try:
        prev = pd.read_csv(f, dtype=str, keep_default_na=False)
    except Exception as exc:
        return jsonify({"error": f"Could not read file: {exc}"}), 400

    if "Surname" not in prev.columns or "First Name" not in prev.columns:
        return jsonify({"error": "File must have Surname and First Name columns"}), 400

    source = enriched_path if os.path.exists(enriched_path) else master_path
    if not os.path.exists(source):
        return jsonify({"error": "No master CSV found — run Stage 2 first"}), 400

    master = pd.read_csv(source, dtype=str, keep_default_na=False)

    from stages.apollo_ingest import APOLLO_INTERNAL_COLS
    for col in APOLLO_INTERNAL_COLS:
        if col not in master.columns:
            master[col] = ""

    # Build lookup on previous file: (surname, firstname, dob) → row index
    prev_key = (
        prev["Surname"].str.strip().str.lower() + "|"
        + prev["First Name"].str.strip().str.lower() + "|"
        + prev.get("Officer date of birth", pd.Series([""] * len(prev))).str.strip()
    )
    prev_lookup = {}
    for i, key in enumerate(prev_key):
        if key not in prev_lookup:
            prev_lookup[key] = i

    curr_key = (
        master["Surname"].str.strip().str.lower() + "|"
        + master["First Name"].str.strip().str.lower() + "|"
        + master.get("Officer date of birth", pd.Series([""] * len(master))).str.strip()
    )

    matched = 0
    for i, key in enumerate(curr_key):
        if key in prev_lookup:
            src_i = prev_lookup[key]
            for col in APOLLO_INTERNAL_COLS:
                if col in prev.columns:
                    val = str(prev.at[src_i, col]).strip()
                    if val:
                        master.at[i, col] = val
            matched += 1

    master.to_csv(enriched_path, index=False)
    db.update_stage4_status(project_id, "running")
    db.add_log(project_id, 4, f"Prefilled {matched:,} rows from previous export")
    return redirect(url_for("project_detail", project_id=project_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 5 — Y/T/N classifier
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/stage5/run", methods=["POST"])
def stage5_run(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    err = _stage_gate(project, 4)
    if err:
        return err

    job = _job(project_id)
    if job and job.get("stage") == 5 and job.get("status") == "running":
        return jsonify({"error": "Classifier already running"}), 409

    # Form flag: ticked by default in the template. Operator unticks when they
    # already know Gemini is unavailable and want to go straight to manual.
    use_gemini = request.form.get("use_gemini") == "1"

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    # If the operator wants Gemini, ping it first so we fail fast on bad-key /
    # quota-exhausted / network. The probe is cheap (one trivial generate call).
    if use_gemini:
        status, details = gemini_health.ping(GEMINI_API_KEY)
        if status != "ok":
            db.update_stage5_status(project_id, "gemini_unavailable")
            db.add_log(project_id, 5, f"Pre-flight: {gemini_health.human(status)} — {details}")
            _set_job(project_id, {"stage": 5, "status": "gemini_unavailable",
                                  "progress": gemini_health.human(status),
                                  "detail": details})
            return redirect(url_for("project_detail", project_id=project_id))

    _set_job(project_id, {"stage": 5, "status": "running", "progress": "Starting…"})
    db.update_stage5_status(project_id, "running")
    ev = threading.Event()
    _cancel_events[project_id] = ev
    api_key = GEMINI_API_KEY if use_gemini else ""
    threading.Thread(target=_run_classifier, args=(project_id, pdir, rc, ev, api_key),
                     daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


def _run_classifier(project_id, pdir, rc, cancel_event, gemini_api_key):
    def cb(msg):
        db.add_log(project_id, 5, msg)
        _update_job(project_id, progress=msg)

    try:
        summary = run_passes_1_and_2(project_id, pdir, rc, gemini_api_key,
                                     progress_cb=cb, db=db, cancel_event=cancel_event)
        _cancel_events.pop(project_id, None)
        if cancel_event.is_set():
            db.update_stage5_status(project_id, "paused")
            db.add_log(project_id, 5, "Classifier paused")
            _update_job(project_id, status="paused")
            return

        # Gemini died mid-run (3 consecutive batch errors). Don't transition to
        # review_needed — the operator might want to retry once credits are back.
        if summary.get("aborted_gemini"):
            db.update_stage5_status(project_id, "gemini_unavailable")
            db.add_log(project_id, 5, "Gemini aborted mid-run — choose Retry or Continue manually")
            _update_job(project_id, status="gemini_unavailable",
                        progress="Gemini aborted — choose Retry or Continue manually")
            return

        review_count = len(db.get_s5_review_queue(project_id))
        if review_count > 0:
            db.update_stage5_status(project_id, "review_needed")
            db.add_log(project_id, 5, f"Passes 1+2 done — {review_count:,} rows need human review")
            _update_job(project_id, status="review_needed",
                        progress=f"{review_count:,} rows in review queue")
        else:
            rows = apply_decisions_and_save(project_id, pdir, rc, db)
            db.update_stage5_status(project_id, "complete")
            db.add_log(project_id, 5, f"Stage 5 complete — {rows:,} rows classified")
            _update_job(project_id, status="done")
    except Exception as exc:
        _cancel_events.pop(project_id, None)
        db.update_stage5_status(project_id, "error")
        db.add_log(project_id, 5, f"ERROR: {exc}")
        _update_job(project_id, status="error", progress=str(exc))


@app.route("/project/<int:project_id>/stage5/continue-manually", methods=["POST"])
def stage5_continue_manually(project_id):
    """
    Operator chose 'Continue without Gemini' on the gemini_unavailable screen.
    Re-runs Pass 1+2 with an empty key, which routes every Tentative row to
    the manual review queue.
    """
    project = db.get_project(project_id)
    if not project:
        abort(404)

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    _set_job(project_id, {"stage": 5, "status": "running",
                          "progress": "Manual mode — skipping Gemini…"})
    db.update_stage5_status(project_id, "running")
    ev = threading.Event()
    _cancel_events[project_id] = ev
    threading.Thread(target=_run_classifier, args=(project_id, pdir, rc, ev, ""),
                     daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage5/retry-gemini", methods=["POST"])
def stage5_retry_gemini(project_id):
    """Re-run Pass 2 only on rows that errored or were never tried."""
    project = db.get_project(project_id)
    if not project:
        abort(404)

    job = _job(project_id)
    if job and job.get("stage") == 5 and job.get("status") == "running":
        return jsonify({"error": "Classifier already running"}), 409

    status, details = gemini_health.ping(GEMINI_API_KEY)
    if status != "ok":
        db.update_stage5_status(project_id, "gemini_unavailable")
        db.add_log(project_id, 5, f"Retry pre-flight failed: {gemini_health.human(status)} — {details}")
        _set_job(project_id, {"stage": 5, "status": "gemini_unavailable",
                              "progress": gemini_health.human(status),
                              "detail": details})
        return redirect(url_for("project_detail", project_id=project_id))

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    _set_job(project_id, {"stage": 5, "status": "running", "progress": "Retrying Gemini…"})
    db.update_stage5_status(project_id, "running")
    ev = threading.Event()
    _cancel_events[project_id] = ev

    def worker():
        def cb(msg):
            db.add_log(project_id, 5, msg)
            _update_job(project_id, progress=msg)
        try:
            summary = classifier_retry(project_id, pdir, rc, GEMINI_API_KEY,
                                       db=db, progress_cb=cb, cancel_event=ev)
            _cancel_events.pop(project_id, None)
            if summary.get("aborted_gemini"):
                db.update_stage5_status(project_id, "gemini_unavailable")
                db.add_log(project_id, 5, "Retry hit consecutive Gemini errors again")
                _update_job(project_id, status="gemini_unavailable",
                            progress="Gemini still failing")
                return
            review_count = len(db.get_s5_review_queue(project_id))
            if review_count > 0:
                db.update_stage5_status(project_id, "review_needed")
                _update_job(project_id, status="review_needed",
                            progress=f"{review_count:,} rows still in review")
            else:
                rows = apply_decisions_and_save(project_id, pdir, rc, db)
                db.update_stage5_status(project_id, "complete")
                db.add_log(project_id, 5, f"Stage 5 complete after retry — {rows:,} rows classified")
                _update_job(project_id, status="done")
        except Exception as exc:
            _cancel_events.pop(project_id, None)
            db.update_stage5_status(project_id, "error")
            db.add_log(project_id, 5, f"Retry ERROR: {exc}")
            _update_job(project_id, status="error", progress=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage5/review")
def stage5_review(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    queue = db.get_s5_review_queue(project_id)

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    enriched = os.path.join(pdir, f"master_{rc}_enriched.csv")
    name_lookup = {}
    if os.path.exists(enriched):
        df = pd.read_csv(enriched, dtype=str, keep_default_na=False,
                         usecols=["Unique ID", "Officer name", "Company Name",
                                  "Apollo First Name", "Apollo Last Name"])
        for _, row in df.iterrows():
            name_lookup[row["Unique ID"]] = {
                "officer_name": row.get("Officer name", ""),
                "company_name": row.get("Company Name", ""),
                "apollo_first": row.get("Apollo First Name", ""),
                "apollo_last":  row.get("Apollo Last Name", ""),
            }

    # The Pass-1 Tentative score is the most useful signal when Gemini didn't
    # give an opinion. Tag each row with where its context came from so the
    # operator can power through manual-mode queues faster.
    pass1_scores = db.get_s5_pass1_scores(project_id)
    failed_count = 0
    gemini_count = 0
    for item in queue:
        info = name_lookup.get(item["unique_id"], {})
        item["officer_name"] = info.get("officer_name", "")
        item["company_name"] = info.get("company_name", "")
        item["apollo_first"] = info.get("apollo_first", "")
        item["apollo_last"]  = info.get("apollo_last", "")

        item["fuzzy_score"] = pass1_scores.get(item["unique_id"])

        reason = item.get("reason") or ""
        if reason.startswith("Gemini error"):
            item["source"] = "Gemini error"
            failed_count += 1
        elif reason.startswith("Gemini not configured"):
            item["source"] = "No Gemini"
            failed_count += 1
        else:
            item["source"] = "Gemini low-conf"
            gemini_count += 1

    # Highest-similarity Tentatives first — fastest to confirm by eye.
    queue.sort(key=lambda r: (r.get("fuzzy_score") or 0), reverse=True)

    # How many of the current queue would auto-N now under the placeholder
    # check? Powers the "Clear N rows with no Apollo data" button.
    placeholder_count = count_apollo_placeholders_in_queue(project_id, pdir, rc, db)

    return render_template("stage5_review.html", project=project, queue=queue,
                           failed_count=failed_count, gemini_count=gemini_count,
                           placeholder_count=placeholder_count)


@app.route("/project/<int:project_id>/stage5/clear-placeholders", methods=["POST"])
def stage5_clear_placeholders(project_id):
    """
    Auto-N every row currently in the review queue whose Apollo data is
    placeholder-only (no useful name → can't outreach). Logs pass_num=3 with
    a clear audit reason. If the queue empties, finishes Stage 5.
    """
    project = db.get_project(project_id)
    if not project:
        abort(404)

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    def cb(msg):
        db.add_log(project_id, 5, msg)

    cleared = reclassify_apollo_placeholders(project_id, pdir, rc, db, progress_cb=cb)

    remaining = db.get_s5_review_queue(project_id)
    if not remaining and cleared > 0:
        rows = apply_decisions_and_save(project_id, pdir, rc, db)
        db.update_stage5_status(project_id, "complete")
        db.add_log(project_id, 5, f"Stage 5 complete — {rows:,} rows classified (placeholder sweep cleared the queue)")

    return redirect(url_for("stage5_review", project_id=project_id))


@app.route("/project/<int:project_id>/stage5/submit-review", methods=["POST"])
def stage5_submit_review(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)

    # Radio buttons are submitted as decision_<unique_id> = Y|T|N
    for key, value in request.form.items():
        if not key.startswith("decision_"):
            continue
        uid = key[len("decision_"):]
        label = value.strip().upper()
        if label in ("Y", "T", "N"):
            db.log_s5_decision(project_id, uid, 3, label, reason="Human review")

    # Check if review queue is now empty
    remaining = db.get_s5_review_queue(project_id)
    if not remaining:
        pdir = db.project_dir(project_id, project["region_code"])
        rc = project["region_code"]
        rows = apply_decisions_and_save(project_id, pdir, rc, db)
        db.update_stage5_status(project_id, "complete")
        db.add_log(project_id, 5, f"Stage 5 complete — {rows:,} rows classified (human review done)")

    return redirect(url_for("project_detail", project_id=project_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 6 — VoteSource pause/resume
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/stage6/export-vs", methods=["POST"])
def stage6_export_vs(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    err = _stage_gate(project, 5)
    if err:
        return err

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    try:
        out_path = build_vs_export(
            pdir, rc,
            progress_cb=lambda m: db.add_log(project_id, 6, m),
        )
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400

    db.set_vs_sent(project_id)

    return send_file(
        out_path,
        as_attachment=True,
        download_name=f"vs_export_{rc}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/project/<int:project_id>/stage6/upload-return", methods=["POST"])
def stage6_upload_return(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    try:
        vs_return = pd.read_csv(f, dtype=str, keep_default_na=False) if f.filename.lower().endswith(".csv") \
                    else pd.read_excel(f, dtype=str, keep_default_na=False)
    except Exception as exc:
        return jsonify({"error": f"Could not read VS return file: {exc}"}), 400

    # VS uses UniqueID (no space); our master uses Unique ID. Accept either.
    if "UniqueID" in vs_return.columns and "Unique ID" not in vs_return.columns:
        vs_return = vs_return.rename(columns={"UniqueID": "Unique ID"})
    if "Unique ID" not in vs_return.columns:
        return jsonify({
            "error": "VS return file must have a 'Unique ID' or 'UniqueID' column"
        }), 400

    # Drop the blank spacer column at position N (header is NaN / empty / "Unnamed: 13")
    drop_cols = [c for c in vs_return.columns
                 if not c or (isinstance(c, str) and c.startswith("Unnamed:"))]
    if drop_cols:
        vs_return = vs_return.drop(columns=drop_cols)

    classified = os.path.join(pdir, f"master_{rc}_classified.csv")
    master = pd.read_csv(classified, dtype=str, keep_default_na=False)

    # Add VS columns to master by Unique ID join
    vs_new_cols = [c for c in vs_return.columns if c != "Unique ID" and c not in master.columns]
    vs_join = vs_return[["Unique ID"] + vs_new_cols]
    master = master.merge(vs_join, on="Unique ID", how="left")
    master[vs_new_cols] = master[vs_new_cols].fillna("")

    out_path = os.path.join(pdir, f"master_{rc}_vs.csv")
    master.to_csv(out_path, index=False)
    db.update_stage6_status(project_id, "complete")
    db.add_log(project_id, 6, f"VS return integrated — {len(vs_new_cols)} new column(s)")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage6/skip", methods=["POST"])
def stage6_skip(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    db.mark_stage_skipped(project_id, 6)
    db.add_log(project_id, 6, "VoteSource stage skipped")
    return redirect(url_for("project_detail", project_id=project_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 7 — Raiser's Edge fuzzy flagger
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/stage7/upload-re", methods=["POST"])
def stage7_upload_re(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    err = _stage_gate(project, 6)
    if err:
        return err
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]

    re_path = os.path.join(pdir, f"re_export_{rc}{os.path.splitext(f.filename)[1]}")
    f.save(re_path)

    job = _job(project_id)
    if job and job.get("stage") == 7 and job.get("status") == "running":
        return jsonify({"error": "RE matching already running"}), 409

    use_gemini = request.form.get("use_gemini") == "1"

    if use_gemini:
        status, details = gemini_health.ping(GEMINI_API_KEY)
        if status != "ok":
            db.update_stage7_status(project_id, "gemini_unavailable")
            db.add_log(project_id, 7, f"Pre-flight: {gemini_health.human(status)} — {details}")
            _set_job(project_id, {"stage": 7, "status": "gemini_unavailable",
                                  "progress": gemini_health.human(status),
                                  "detail": details, "re_path": re_path})
            return redirect(url_for("project_detail", project_id=project_id))

    _set_job(project_id, {"stage": 7, "status": "running",
                          "progress": "Starting RE match…", "re_path": re_path})
    db.update_stage7_status(project_id, "running")
    ev = threading.Event()
    _cancel_events[project_id] = ev
    api_key = GEMINI_API_KEY if use_gemini else ""
    threading.Thread(target=_run_re_flagger,
                     args=(project_id, pdir, rc, re_path, ev, api_key), daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


def _run_re_flagger(project_id, pdir, rc, re_path, cancel_event, gemini_api_key):
    def cb(msg):
        db.add_log(project_id, 7, msg)
        _update_job(project_id, progress=msg)

    try:
        summary = run_re_flagging(project_id, pdir, rc, re_path, gemini_api_key,
                                  progress_cb=cb, db=db, cancel_event=cancel_event)
        _cancel_events.pop(project_id, None)
        if cancel_event.is_set():
            db.update_stage7_status(project_id, "paused")
            db.add_log(project_id, 7, "RE matching paused")
            _update_job(project_id, status="paused")
            return

        if summary.get("aborted_gemini"):
            db.update_stage7_status(project_id, "gemini_unavailable")
            db.add_log(project_id, 7, "Gemini aborted mid-run — choose Retry or Continue manually")
            _update_job(project_id, status="gemini_unavailable",
                        progress="Gemini aborted — choose Retry or Continue manually",
                        re_path=re_path)
            return

        review_count = len(db.get_s7_review_queue(project_id))
        if review_count > 0:
            db.update_stage7_status(project_id, "review_needed")
            db.add_log(project_id, 7, f"RE matching done — {review_count:,} rows need human review")
            _update_job(project_id, status="review_needed",
                        progress=f"{review_count:,} rows in review queue")
        else:
            rows = apply_re_decisions_and_save(project_id, pdir, rc, db)
            db.update_stage7_status(project_id, "complete")
            db.add_log(project_id, 7, f"Stage 7 complete — {rows:,} rows")
            _update_job(project_id, status="done")
    except Exception as exc:
        _cancel_events.pop(project_id, None)
        db.update_stage7_status(project_id, "error")
        db.add_log(project_id, 7, f"ERROR: {exc}")
        _update_job(project_id, status="error", progress=str(exc))


@app.route("/project/<int:project_id>/stage7/continue-manually", methods=["POST"])
def stage7_continue_manually(project_id):
    """Re-run RE flagging with Gemini disabled. The RE export path is stashed on the job."""
    project = db.get_project(project_id)
    if not project:
        abort(404)

    job = _job(project_id) or {}
    re_path = job.get("re_path")
    if not re_path or not os.path.exists(re_path):
        # Fall back to looking up the saved RE export in the project dir.
        pdir = db.project_dir(project_id, project["region_code"])
        matches = sorted(glob.glob(os.path.join(pdir, f"re_export_{project['region_code']}.*")))
        if not matches:
            return jsonify({"error": "RE export file not found — re-upload it."}), 400
        re_path = matches[0]

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    _set_job(project_id, {"stage": 7, "status": "running",
                          "progress": "Manual mode — skipping Gemini…", "re_path": re_path})
    db.update_stage7_status(project_id, "running")
    ev = threading.Event()
    _cancel_events[project_id] = ev
    threading.Thread(target=_run_re_flagger,
                     args=(project_id, pdir, rc, re_path, ev, ""), daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage7/retry-gemini", methods=["POST"])
def stage7_retry_gemini(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)

    job = _job(project_id)
    if job and job.get("stage") == 7 and job.get("status") == "running":
        return jsonify({"error": "RE matching already running"}), 409

    status, details = gemini_health.ping(GEMINI_API_KEY)
    if status != "ok":
        db.update_stage7_status(project_id, "gemini_unavailable")
        db.add_log(project_id, 7, f"Retry pre-flight failed: {gemini_health.human(status)} — {details}")
        _set_job(project_id, {"stage": 7, "status": "gemini_unavailable",
                              "progress": gemini_health.human(status), "detail": details})
        return redirect(url_for("project_detail", project_id=project_id))

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    _set_job(project_id, {"stage": 7, "status": "running", "progress": "Retrying Gemini…"})
    db.update_stage7_status(project_id, "running")
    ev = threading.Event()
    _cancel_events[project_id] = ev

    def worker():
        def cb(msg):
            db.add_log(project_id, 7, msg)
            _update_job(project_id, progress=msg)
        try:
            summary = re_flagger_retry(project_id, pdir, rc, GEMINI_API_KEY,
                                       db=db, progress_cb=cb, cancel_event=ev)
            _cancel_events.pop(project_id, None)
            if summary.get("aborted_gemini"):
                db.update_stage7_status(project_id, "gemini_unavailable")
                db.add_log(project_id, 7, "Retry hit consecutive Gemini errors again")
                _update_job(project_id, status="gemini_unavailable",
                            progress="Gemini still failing")
                return
            review_count = len(db.get_s7_review_queue(project_id))
            if review_count > 0:
                db.update_stage7_status(project_id, "review_needed")
                _update_job(project_id, status="review_needed",
                            progress=f"{review_count:,} rows still in review")
            else:
                rows = apply_re_decisions_and_save(project_id, pdir, rc, db)
                db.update_stage7_status(project_id, "complete")
                db.add_log(project_id, 7, f"Stage 7 complete after retry — {rows:,} rows")
                _update_job(project_id, status="done")
        except Exception as exc:
            _cancel_events.pop(project_id, None)
            db.update_stage7_status(project_id, "error")
            db.add_log(project_id, 7, f"Retry ERROR: {exc}")
            _update_job(project_id, status="error", progress=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage7/review")
def stage7_review(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    queue = db.get_s7_review_queue(project_id)

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    for suffix in ["vs", "classified", "enriched", "raw"]:
        mp = os.path.join(pdir, f"master_{rc}_{suffix}.csv")
        if os.path.exists(mp):
            df = pd.read_csv(mp, dtype=str, keep_default_na=False,
                             usecols=["Unique ID", "Officer name", "Company Name"])
            lookup = {r["Unique ID"]: r for _, r in df.iterrows()}
            for item in queue:
                info = lookup.get(item["unique_id"], {})
                item["officer_name"] = info.get("Officer name", "")
                item["company_name"] = info.get("Company Name", "")
            break

    pass1_scores = db.get_s7_pass1_scores(project_id)
    failed_count = 0
    for item in queue:
        item["fuzzy_score"] = pass1_scores.get(item["unique_id"])

        reason = item.get("reason") or ""
        if reason.startswith("Gemini error"):
            item["source"] = "Gemini error"
            failed_count += 1
        elif reason.startswith("Gemini not configured"):
            item["source"] = "No Gemini"
            failed_count += 1
        else:
            item["source"] = "Gemini low-conf"

    queue.sort(key=lambda r: (r.get("fuzzy_score") or 0), reverse=True)

    return render_template("stage7_review.html", project=project, queue=queue,
                           failed_count=failed_count)


@app.route("/project/<int:project_id>/stage7/submit-review", methods=["POST"])
def stage7_submit_review(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)

    # Radio buttons are submitted as decision_<unique_id> = match|no_match
    for key, value in request.form.items():
        if not key.startswith("decision_"):
            continue
        uid = key[len("decision_"):]
        label = value.strip()
        if label in ("match", "no_match"):
            db.log_s7_decision(project_id, uid, "", "person", label,
                               reason="Human review", pass_num=3)

    remaining = db.get_s7_review_queue(project_id)
    if not remaining:
        pdir = db.project_dir(project_id, project["region_code"])
        rc = project["region_code"]
        rows = apply_re_decisions_and_save(project_id, pdir, rc, db)
        db.update_stage7_status(project_id, "complete")
        db.add_log(project_id, 7, f"Stage 7 complete — {rows:,} rows (human review done)")

    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/project/<int:project_id>/stage7/skip", methods=["POST"])
def stage7_skip(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    db.mark_stage_skipped(project_id, 7)
    db.add_log(project_id, 7, "RE matching stage skipped")
    return redirect(url_for("project_detail", project_id=project_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Stage 8 — Final export
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/project/<int:project_id>/stage8/export", methods=["POST"])
def stage8_export(project_id):
    project = db.get_project(project_id)
    if not project:
        abort(404)
    err = _stage_gate(project, 7)
    if err:
        return err

    pdir = db.project_dir(project_id, project["region_code"])
    rc = project["region_code"]
    db.update_stage8_status(project_id, "running")

    try:
        out_path = build_export(pdir, rc,
                                progress_cb=lambda m: db.add_log(project_id, 8, m))
        db.update_stage8_status(project_id, "complete")
        db.add_log(project_id, 8, f"Stage 8 complete — {os.path.basename(out_path)}")
    except Exception as exc:
        db.update_stage8_status(project_id, "error")
        db.add_log(project_id, 8, f"ERROR: {exc}")
        return jsonify({"error": str(exc)}), 500

    return redirect(url_for("project_detail", project_id=project_id))


if __name__ == "__main__":
    app.run(debug=True, port=5050)
