"""
Stage 5 — Y/T/N sanity check (does Apollo's match really refer to this person?).

What it does, in plain English:
  For every Apollo-matched row, decide whether the Apollo company name
  actually matches the Companies House company name — i.e. did Apollo
  find the right person, but at the wrong company. Three-tier cascade:
    Pass 1: deterministic string similarity (rapidfuzz token-sort ratio)
            ≥85 → Yes, <45 → No, everything in between → Tentative
    Pass 2: Gemini Flash judges the Tentative band in batches of 20,
            returns label (Y/T/N) + confidence (0–1) + one-line reason;
            confidence ≥0.80 auto-applies
    Pass 3: only rows still uncertain reach the operator's web UI
  Every decision is written to the stage5_decisions DB table and to
  classifications_log.csv for audit.

What's different from the old process:
  Replaces Steps 9 and 15. Used to be: every Apollo-matched row got a
  human Y/T/N decision — ~7,200 rows for Leicester. With this cascade
  the operator only sees a few hundred genuinely ambiguous ones; the
  obvious Yes and No are auto-resolved and Gemini handles the rest.
"""
import os
import json
import csv
import re
import time
import pandas as pd
from rapidfuzz import fuzz
import google.generativeai as genai

# Score thresholds
PASS1_YES = 85    # token_sort_ratio >= this → Y
PASS1_NO  = 45    # token_sort_ratio < this  → N (middle band → Tentative for Pass 2)
PASS2_AUTO = 0.70  # Gemini confidence >= this → auto-apply (dropped from 0.80
                  # so fewer reasonable-but-not-certain matches escalate to human)
PASS2_BATCH = 20   # row-pairs per Gemini call

# Placeholder strings Apollo writes when it couldn't enrich a row. Treated as
# empty when building the Apollo name, so a row with "N/A N/A" lands in
# Pass-1 auto-N (can't outreach without contact info) instead of fuzzy-matching
# into the Tentative band and clogging the human review queue.
APOLLO_NULL = {"", "n/a", "na", "n\\a", "-", "—", "none", "null", "."}

GEMINI_MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """You are a data-quality assistant for a UK political campaign database.
Your task: decide whether an Apollo-enriched record refers to the same person as a UK Companies House officer record.

Rules:
- Y = strong evidence it is the same person (names clearly match)
- T = plausible but uncertain (partial name match, common name, etc.)
- N = different person or no real match

Respond ONLY with a valid JSON array — no prose, no markdown code fences, no extra text.
Each element must have exactly these keys: unique_id, label, confidence, reason.
confidence is a float 0.0–1.0. reason is one short sentence."""

LOG_COLS = ["unique_id", "pass_num", "label", "confidence", "reason", "officer_name", "apollo_name"]


def _normalise(name):
    """Strip punctuation, lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", name.lower())).strip()


def _apollo_name(row):
    first = str(row.get("Apollo First Name", "")).strip()
    last = str(row.get("Apollo Last Name", "")).strip()
    if first.lower() in APOLLO_NULL:
        first = ""
    if last.lower() in APOLLO_NULL:
        last = ""
    return f"{first} {last}".strip()


def _append_log(log_path, entries):
    write_header = not os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_COLS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(entries)


def run_passes_1_and_2(project_id, project_dir, region_code, gemini_api_key, progress_cb=None, db=None, cancel_event=None):
    """
    Run Pass 1 (deterministic) and Pass 2 (Gemini) of the classifier.
    Writes decisions to the DB and classifications_log.csv.
    Returns a summary dict.

    `db` is the database module (passed in to avoid circular import).
    """

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")
    if not os.path.exists(enriched_path):
        raise FileNotFoundError(f"master_{region_code}_enriched.csv not found — run Stage 4 first.")

    master = pd.read_csv(enriched_path, dtype=str, keep_default_na=False)
    log(f"Loaded enriched master: {len(master):,} rows")

    log_path = os.path.join(project_dir, "classifications_log.csv")
    log_entries = []

    # ── Pass 1: deterministic ────────────────────────────────────────────────
    pass1_y = pass1_n = pass1_t = 0
    tentative_rows = []

    for _, row in master.iterrows():
        uid = row["Unique ID"]
        officer = str(row.get("Officer name", "")).strip()
        apollo = _apollo_name(row)

        if not apollo.strip():
            label = "N"
            reason = "No Apollo hit"
            db.log_s5_decision(project_id, uid, 1, label, reason=reason)
            log_entries.append({"unique_id": uid, "pass_num": 1, "label": label,
                                 "confidence": None, "reason": reason,
                                 "officer_name": officer, "apollo_name": apollo})
            pass1_n += 1
            continue

        score = fuzz.token_sort_ratio(_normalise(officer), _normalise(apollo))

        if score >= PASS1_YES:
            label = "Y"
            reason = f"Name similarity {score}"
            db.log_s5_decision(project_id, uid, 1, label, confidence=score / 100, reason=reason)
            log_entries.append({"unique_id": uid, "pass_num": 1, "label": label,
                                 "confidence": score / 100, "reason": reason,
                                 "officer_name": officer, "apollo_name": apollo})
            pass1_y += 1
        elif score < PASS1_NO:
            label = "N"
            reason = f"Name similarity {score} — too low"
            db.log_s5_decision(project_id, uid, 1, label, confidence=score / 100, reason=reason)
            log_entries.append({"unique_id": uid, "pass_num": 1, "label": label,
                                 "confidence": score / 100, "reason": reason,
                                 "officer_name": officer, "apollo_name": apollo})
            pass1_n += 1
        else:
            # Log the Tentative fuzzy score as a Pass-1 record so the review
            # screen can display it later. This row will be overwritten by the
            # Pass-2 (or Pass-3) decision in `get_s5_decisions`, so it doesn't
            # affect the final Result column.
            db.log_s5_decision(project_id, uid, 1, "T", confidence=score / 100,
                               reason=f"Tentative — fuzzy score {score}")
            log_entries.append({"unique_id": uid, "pass_num": 1, "label": "T",
                                 "confidence": score / 100,
                                 "reason": f"Tentative — fuzzy score {score}",
                                 "officer_name": officer, "apollo_name": apollo})
            tentative_rows.append({
                "unique_id": uid,
                "officer_name": officer,
                "apollo_name": apollo,
                "company_name": str(row.get("Company Name", "")),
            })
            pass1_t += 1

    _append_log(log_path, log_entries)
    log(f"Pass 1 done: {pass1_y:,} Y | {pass1_n:,} N | {pass1_t:,} Tentative → Pass 2")

    # ── Pass 2: Gemini Flash ──────────────────────────────────────────────────
    if not tentative_rows:
        log("No Tentative rows — skipping Gemini pass")
        return {"pass1_y": pass1_y, "pass1_n": pass1_n, "pass1_t": pass1_t,
                "pass2_auto": 0, "pass2_review": 0, "aborted_gemini": False}

    if not gemini_api_key:
        log("WARNING: GEMINI_API_KEY not set — all Tentative rows go to human review")
        for r in tentative_rows:
            db.log_s5_decision(project_id, r["unique_id"], 2, "T", confidence=0.0,
                               reason="Gemini not configured — human review required")
        return {"pass1_y": pass1_y, "pass1_n": pass1_n, "pass1_t": pass1_t,
                "pass2_auto": 0, "pass2_review": len(tentative_rows),
                "aborted_gemini": False}

    pass2_auto, pass2_review, pass2_errors, aborted = _run_gemini_pass2(
        project_id, tentative_rows, gemini_api_key, log_path, db, log, cancel_event,
    )

    return {
        "pass1_y": pass1_y, "pass1_n": pass1_n, "pass1_t": pass1_t,
        "pass2_auto": pass2_auto, "pass2_review": pass2_review,
        "aborted_gemini": aborted,
    }


# Three consecutive batch errors → assume Gemini is down and stop hammering it.
CONSECUTIVE_ERROR_LIMIT = 3

# Pace Gemini calls so a multi-batch region stays under the free-tier RPM cap,
# and on a 429 honor the server's retry_delay and retry the SAME batch instead
# of discarding its rows. The caps stop a genuine outage from hanging — once a
# batch's retries are exhausted it falls through to the consecutive-error abort.
PASS2_MIN_INTERVAL = 4.0       # seconds to wait between successive Gemini calls
PASS2_MAX_BATCH_RETRIES = 4    # rate-limit retries per batch before giving up
PASS2_MAX_BACKOFF = 70         # cap (seconds) on any single retry_delay wait

# Gemini's ResourceExhausted carries a "retry_delay { seconds: N }" hint.
_RETRY_DELAY_RE = re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)", re.I)


def _is_rate_limit(exc):
    """True if the exception looks like a Gemini quota / 429 rate-limit error."""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    return ("429" in msg or "quota" in msg or "rate limit" in msg or "rate-limit" in msg
            or "resourceexhausted" in name or "resource_exhausted" in msg)


def _retry_delay_seconds(exc, default=30):
    """Pull the server-suggested retry_delay (seconds) out of a 429, else default."""
    m = _RETRY_DELAY_RE.search(str(exc))
    return int(m.group(1)) if m else default


def _run_gemini_pass2(project_id, rows, gemini_api_key, log_path, db, log, cancel_event):
    """
    Run Gemini Pass 2 on a list of {unique_id, officer_name, apollo_name, company_name}
    dicts. Returns (auto, review, errors, aborted_gemini).
    Shared by the initial run and the retry-failed-rows path.
    """
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    pass2_auto = pass2_review = pass2_errors = 0
    log_entries = []
    consecutive_errors = 0
    aborted = False

    batches = [rows[i:i+PASS2_BATCH] for i in range(0, len(rows), PASS2_BATCH)]
    log(f"Pass 2: {len(rows):,} rows in {len(batches)} Gemini batch(es)")

    for b_idx, batch in enumerate(batches):
        prompt = SYSTEM_PROMPT + "\n\nItems:\n" + json.dumps(batch, ensure_ascii=False)

        # Pace requests so a multi-batch region stays under Gemini's RPM cap.
        # (No wait before the very first call.)
        if b_idx > 0:
            time.sleep(PASS2_MIN_INTERVAL)

        # Attempt the batch, retrying on a 429 by honoring the server's
        # retry_delay rather than throwing the rows to review. Non-rate-limit
        # errors are not retried — they fall straight through to the handler.
        decisions = None
        last_exc = None
        for attempt in range(PASS2_MAX_BATCH_RETRIES + 1):
            try:
                resp = model.generate_content(prompt)
                raw = resp.text.strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```[a-z]*\n?", "", raw)
                    raw = re.sub(r"\n?```$", "", raw)
                decisions = json.loads(raw)
                break
            except Exception as exc:
                last_exc = exc
                if _is_rate_limit(exc) and attempt < PASS2_MAX_BATCH_RETRIES:
                    wait = min(_retry_delay_seconds(exc), PASS2_MAX_BACKOFF)
                    log(f"  Batch {b_idx + 1} rate-limited — waiting {wait}s then retrying "
                        f"({attempt + 1}/{PASS2_MAX_BATCH_RETRIES})")
                    time.sleep(wait)
                    if cancel_event and cancel_event.is_set():
                        break
                    continue
                break

        if decisions is not None:
            for d in decisions:
                uid = d.get("unique_id", "")
                label = str(d.get("label", "T")).strip().upper()
                confidence = float(d.get("confidence", 0.0))
                reason = str(d.get("reason", ""))

                if label not in ("Y", "T", "N"):
                    label = "T"

                db.log_s5_decision(project_id, uid, 2, label, confidence=confidence, reason=reason)
                log_entries.append({
                    "unique_id": uid, "pass_num": 2, "label": label,
                    "confidence": confidence, "reason": reason,
                    "officer_name": next((r["officer_name"] for r in batch if r["unique_id"] == uid), ""),
                    "apollo_name": next((r["apollo_name"] for r in batch if r["unique_id"] == uid), ""),
                })

                if confidence >= PASS2_AUTO:
                    pass2_auto += 1
                else:
                    pass2_review += 1

            consecutive_errors = 0

        else:
            # Retries exhausted on a persistent 429, or a non-retryable error.
            log(f"  Batch {b_idx + 1} Gemini error: {last_exc} — {len(batch)} rows sent to review")
            for r in batch:
                db.log_s5_decision(project_id, r["unique_id"], 2, "T", confidence=0.0,
                                   reason=f"Gemini error: {last_exc}")
            pass2_errors += len(batch)
            pass2_review += len(batch)
            consecutive_errors += 1

            if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                remaining = sum(len(b) for b in batches[b_idx + 1:])
                if remaining:
                    log(f"  {CONSECUTIVE_ERROR_LIMIT} consecutive Gemini errors — aborting Pass 2. "
                        f"{remaining:,} rows left untried (use Retry once Gemini is back).")
                aborted = True
                break

        if (b_idx + 1) % 10 == 0 or b_idx == len(batches) - 1:
            log(f"  Pass 2: {b_idx + 1}/{len(batches)} batches done")

        if cancel_event and cancel_event.is_set():
            log("  Cancelled — stopping after current batch")
            break

    _append_log(log_path, log_entries)
    log(f"Pass 2 done: {pass2_auto:,} auto-applied | {pass2_review:,} to review | {pass2_errors:,} errors"
        + (" (aborted)" if aborted else ""))

    return pass2_auto, pass2_review, pass2_errors, aborted


def retry_gemini_failed_rows(project_id, project_dir, region_code, gemini_api_key,
                              db, progress_cb=None, cancel_event=None):
    """
    Re-run Pass 2 on rows currently sitting in the review queue because Gemini
    errored or was not configured the first time round.

    Returns: {"retried": N, "auto_applied": N, "still_in_review": N, "aborted_gemini": bool}
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)

    if not gemini_api_key:
        log("Cannot retry — GEMINI_API_KEY is still not set.")
        return {"retried": 0, "auto_applied": 0, "still_in_review": 0, "aborted_gemini": True}

    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")
    if not os.path.exists(enriched_path):
        raise FileNotFoundError(f"master_{region_code}_enriched.csv not found — Stage 4 must have run.")

    master = pd.read_csv(enriched_path, dtype=str, keep_default_na=False)
    by_uid = {row["Unique ID"]: row for _, row in master.iterrows()}

    queue = db.get_s5_review_queue(project_id)
    failed = [q for q in queue if (q.get("reason") or "").startswith(("Gemini error", "Gemini not configured"))]
    if not failed:
        log("No Gemini-failed rows in the review queue.")
        return {"retried": 0, "auto_applied": 0, "still_in_review": 0, "aborted_gemini": False}

    log(f"Retrying Gemini on {len(failed):,} failed row(s)")

    rows = []
    for q in failed:
        uid = q["unique_id"]
        row = by_uid.get(uid)
        if row is None:
            continue
        rows.append({
            "unique_id": uid,
            "officer_name": str(row.get("Officer name", "")),
            "apollo_name": _apollo_name(row),
            "company_name": str(row.get("Company Name", "")),
        })

    log_path = os.path.join(project_dir, "classifications_log.csv")
    auto, review, _errors, aborted = _run_gemini_pass2(
        project_id, rows, gemini_api_key, log_path, db, log, cancel_event,
    )

    return {"retried": len(rows), "auto_applied": auto,
            "still_in_review": review, "aborted_gemini": aborted}


def count_apollo_placeholders_in_queue(project_id, project_dir, region_code, db):
    """
    Count how many rows currently in the review queue would now auto-N under
    the placeholder-aware Apollo-name check. Cheap read-only — used to decide
    whether to surface the "Clear N rows" button on the review screen.
    """
    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")
    if not os.path.exists(enriched_path):
        return 0

    master = pd.read_csv(enriched_path, dtype=str, keep_default_na=False)
    by_uid = {row["Unique ID"]: row for _, row in master.iterrows()}

    queue = db.get_s5_review_queue(project_id)
    count = 0
    for q in queue:
        row = by_uid.get(q["unique_id"])
        if row is None:
            continue
        if not _apollo_name(row):
            count += 1
    return count


def reclassify_apollo_placeholders(project_id, project_dir, region_code, db, progress_cb=None):
    """
    Retroactively auto-N every row currently in the review queue whose Apollo
    name normalises to empty under APOLLO_NULL. Logs a pass_num=3 decision
    (label='N') with a clear "Auto-N: Apollo returned no data" reason — pass
    3 is the override pass, so these rows drop out of the review queue
    without losing their audit trail.

    Returns the number of rows cleared.
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)

    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")
    if not os.path.exists(enriched_path):
        raise FileNotFoundError(f"master_{region_code}_enriched.csv not found — Stage 4 must have run.")

    master = pd.read_csv(enriched_path, dtype=str, keep_default_na=False)
    by_uid = {row["Unique ID"]: row for _, row in master.iterrows()}

    queue = db.get_s5_review_queue(project_id)
    cleared = 0
    for q in queue:
        uid = q["unique_id"]
        row = by_uid.get(uid)
        if row is None:
            continue
        if not _apollo_name(row):
            db.log_s5_decision(project_id, uid, 3, "N",
                               reason="Auto-N: Apollo returned no data (placeholder/empty)")
            cleared += 1

    log(f"Reclassified {cleared:,} placeholder row(s) as auto-N")
    return cleared


def apply_decisions_and_save(project_id, project_dir, region_code, db):
    """
    Read all decisions from DB, write the `Result` column to the enriched master,
    and save as master_{REGION}_classified.csv.

    Called after Pass 3 (human review) when the review queue is empty.
    Returns row count.
    """
    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")
    master = pd.read_csv(enriched_path, dtype=str, keep_default_na=False)

    decisions = db.get_s5_decisions(project_id)

    def get_label(uid):
        d = decisions.get(uid)
        return d["label"] if d else "T"

    master["Result"] = master["Unique ID"].apply(get_label)

    out_path = os.path.join(project_dir, f"master_{region_code}_classified.csv")
    master.to_csv(out_path, index=False)
    return len(master)
