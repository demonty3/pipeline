"""
Stage 7 — Raiser's Edge fuzzy flagger (mark which people are existing donors).

What it does, in plain English:
  The operator uploads the Raiser's Edge name export. The RE Name column
  mixes person and company names in the same field, which is the bit that
  makes this stage hard. We:
    - Normalise both sides (strip titles like "Mr"/"Dr" and suffixes
      like "Jr"/"II", lowercase, collapse whitespace)
    - Fuzzy-match every RE entry against every master row using rapidfuzz
    - Bucket each candidate by score:
        ≥90       → auto-flag as a match
        70–89     → Gemini Flash for disambiguation: it first decides
                    whether the RE entry is a person or a company, then
                    whether it's the same entity; ≥0.80 confidence
                    auto-applies, anything else queues for human review
        <70       → don't flag
  Adds RE Match? / Potential / Match? columns to the master and saves
  master_<REGION>_re_flagged.csv.

What's different from the old process:
  Replaces Steps 13–14. Used to be: by-eye XLOOKUP of the RE name list
  against the master, with no way to tell person from company entries.
  Charles called this out explicitly as the stage most worth improving.
"""
import os
import json
import re
import pandas as pd
from rapidfuzz import fuzz
import google.generativeai as genai

SCORE_HIGH = 90    # auto-flag threshold
SCORE_MED  = 70    # Gemini disambiguation threshold (below → no flag)
PASS2_AUTO = 0.80  # Gemini confidence → auto-apply
PASS2_BATCH = 20

GEMINI_MODEL = "gemini-2.5-flash"

TITLE_PREFIXES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "lady", "lord", "rev", "the"}
NAME_SUFFIXES  = {"jr", "sr", "ii", "iii", "iv", "esq"}

SYSTEM_PROMPT = """You are a data-quality assistant for a UK charity donor database (Raiser's Edge).
Decide whether a Raiser's Edge constituent record refers to the same person or company as a UK Companies House officer record.

Rules:
- match = strong evidence they refer to the same entity
- no_match = different person/company or insufficient evidence

Respond ONLY with a valid JSON array — no prose, no markdown fences.
Each element: {unique_id, label, match_type, confidence, reason}
  label: "match" or "no_match"
  match_type: "person" or "company"
  confidence: float 0.0–1.0
  reason: one short sentence"""


def _normalise(name):
    """Strip titles, suffixes, punctuation, lowercase."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", " ", name)
    tokens = [t for t in name.split() if t not in TITLE_PREFIXES and t not in NAME_SUFFIXES]
    return " ".join(tokens)


def _load_re_names(re_path):
    """
    Read RE export (XLSX or ODS) and return a list of normalised name strings.
    Expects a 'Name' column (column 4, index 3).
    """
    ext = os.path.splitext(re_path)[1].lower()
    try:
        if ext == ".ods":
            df = pd.read_excel(re_path, engine="odf", dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(re_path, dtype=str, keep_default_na=False)
    except Exception as e:
        raise ValueError(f"Could not read RE export: {e}")

    # Find the Name column — either by header or position
    if "Name" in df.columns:
        names = df["Name"].dropna().astype(str).tolist()
    elif len(df.columns) >= 4:
        names = df.iloc[:, 3].dropna().astype(str).tolist()
    else:
        raise ValueError("Could not find a Name column in the RE export.")

    return [n.strip() for n in names if n.strip()]


def _pick_master(project_dir, region_code):
    for suffix in ["vs", "classified", "enriched", "raw"]:
        path = os.path.join(project_dir, f"master_{region_code}_{suffix}.csv")
        if os.path.exists(path):
            return path
    raise FileNotFoundError("No suitable master file found for Stage 7.")


def run_re_flagging(project_id, project_dir, region_code, re_path, gemini_api_key,
                    progress_cb=None, db=None, cancel_event=None):
    """
    Run RE fuzzy flagging (Passes 1 + 2).

    Returns summary dict: {auto_flagged, gemini_auto, gemini_review, no_flag}
    """

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    re_names = _load_re_names(re_path)
    log(f"RE export loaded: {len(re_names):,} names")

    master_path = _pick_master(project_dir, region_code)
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    log(f"Master loaded: {len(master):,} rows from {os.path.basename(master_path)}")

    # Pre-normalise RE names once
    re_norm = [_normalise(n) for n in re_names]

    auto_flagged = 0
    gemini_candidates = []  # rows to send to Gemini
    no_flag = 0

    # Pre-normalise master fields
    person_norm = [
        _normalise(str(row.get("Surname", "")) + " " + str(row.get("First Name", "")))
        for _, row in master.iterrows()
    ]
    company_norm = [_normalise(str(row.get("Company Name", ""))) for _, row in master.iterrows()]

    for idx, (p_norm, c_norm) in enumerate(zip(person_norm, company_norm)):
        uid = master.at[idx, "Unique ID"]
        best_score = 0
        best_re_name = ""
        best_match_type = "person"

        for re_raw, re_n in zip(re_names, re_norm):
            p_score = fuzz.partial_ratio(p_norm, re_n)
            c_score = fuzz.partial_ratio(c_norm, re_n)
            score = max(p_score, c_score)
            if score > best_score:
                best_score = score
                best_re_name = re_raw
                best_match_type = "person" if p_score >= c_score else "company"

        if best_score >= SCORE_HIGH:
            db.log_s7_decision(project_id, uid, best_re_name, best_match_type,
                               "match", confidence=best_score / 100,
                               reason=f"Auto-match score {best_score}", pass_num=1)
            auto_flagged += 1
        elif best_score >= SCORE_MED:
            # Log the Pass-1 fuzzy score so the review screen can show it.
            # label='tentative' keeps it distinct from Pass-1 auto-matches.
            db.log_s7_decision(project_id, uid, best_re_name, best_match_type,
                               "tentative", confidence=best_score / 100,
                               reason=f"Tentative — fuzzy score {best_score}", pass_num=1)
            gemini_candidates.append({
                "unique_id": uid,
                "officer_name": master.at[idx, "Officer name"],
                "company_name": master.at[idx, "Company Name"],
                "re_name": best_re_name,
                "_match_type": best_match_type,
                "_score": best_score,
            })
        else:
            no_flag += 1

        if (idx + 1) % 5000 == 0:
            log(f"  Matched {idx + 1:,}/{len(master):,} rows...")

    log(f"Pass 1 done: {auto_flagged:,} auto-flagged | {len(gemini_candidates):,} for Gemini | {no_flag:,} no match")

    # ── Pass 2: Gemini Flash ──────────────────────────────────────────────────
    gemini_auto = gemini_review = 0
    aborted = False

    if gemini_candidates:
        if not gemini_api_key:
            log("WARNING: GEMINI_API_KEY not set — Gemini candidates sent to human review")
            for c in gemini_candidates:
                db.log_s7_decision(project_id, c["unique_id"], c["re_name"], c["_match_type"],
                                   "match", confidence=0.0,
                                   reason="Gemini not configured — human review required", pass_num=2)
            gemini_review = len(gemini_candidates)
        else:
            gemini_auto, gemini_review, _errors, aborted = _run_gemini_pass2(
                project_id, gemini_candidates, gemini_api_key, db, log, cancel_event,
            )

    log(f"Pass 2 done: {gemini_auto:,} auto-applied | {gemini_review:,} to review"
        + (" (aborted)" if aborted else ""))

    return {
        "auto_flagged": auto_flagged,
        "gemini_auto": gemini_auto,
        "gemini_review": gemini_review,
        "no_flag": no_flag,
        "aborted_gemini": aborted,
    }


# Three consecutive batch errors → assume Gemini is down and stop hammering it.
CONSECUTIVE_ERROR_LIMIT = 3


def _run_gemini_pass2(project_id, candidates, gemini_api_key, db, log, cancel_event):
    """
    Run Gemini Pass 2 on a list of candidate dicts (must include unique_id,
    officer_name, company_name, re_name, _match_type).
    Returns (auto, review, errors, aborted_gemini).
    Shared by the initial run and the retry-failed-rows path.
    """
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    gemini_auto = gemini_review = errors = 0
    consecutive_errors = 0
    aborted = False

    batches = [candidates[i:i+PASS2_BATCH] for i in range(0, len(candidates), PASS2_BATCH)]
    log(f"Pass 2: {len(candidates):,} candidates in {len(batches)} Gemini batch(es)")

    for b_idx, batch in enumerate(batches):
        items = [{"unique_id": c["unique_id"], "officer_name": c["officer_name"],
                  "company_name": c["company_name"], "re_name": c["re_name"]}
                 for c in batch]
        prompt = SYSTEM_PROMPT + "\n\nItems:\n" + json.dumps(items, ensure_ascii=False)

        try:
            resp = model.generate_content(prompt)
            raw = resp.text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            decisions = json.loads(raw)

            for d in decisions:
                uid = d.get("unique_id", "")
                label = str(d.get("label", "no_match")).lower()
                match_type = str(d.get("match_type", "person")).lower()
                confidence = float(d.get("confidence", 0.0))
                reason = str(d.get("reason", ""))
                re_name = next((c["re_name"] for c in batch if c["unique_id"] == uid), "")

                db.log_s7_decision(project_id, uid, re_name, match_type,
                                   label, confidence=confidence, reason=reason, pass_num=2)
                if confidence >= PASS2_AUTO:
                    gemini_auto += 1
                else:
                    gemini_review += 1

            consecutive_errors = 0

        except Exception as exc:
            log(f"  Batch {b_idx + 1} Gemini error: {exc}")
            for c in batch:
                db.log_s7_decision(project_id, c["unique_id"], c["re_name"], c["_match_type"],
                                   "match", confidence=0.0,
                                   reason=f"Gemini error: {exc}", pass_num=2)
            gemini_review += len(batch)
            errors += len(batch)
            consecutive_errors += 1

            if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                remaining = sum(len(b) for b in batches[b_idx + 1:])
                if remaining:
                    log(f"  {CONSECUTIVE_ERROR_LIMIT} consecutive Gemini errors — aborting Pass 2. "
                        f"{remaining:,} rows left untried (use Retry once Gemini is back).")
                aborted = True
                break

        if (b_idx + 1) % 5 == 0 or b_idx == len(batches) - 1:
            log(f"  Pass 2: {b_idx + 1}/{len(batches)} batches done")

        if cancel_event and cancel_event.is_set():
            log("  Cancelled — stopping after current batch")
            break

    return gemini_auto, gemini_review, errors, aborted


def retry_gemini_failed_rows(project_id, project_dir, region_code, gemini_api_key,
                              db, progress_cb=None, cancel_event=None):
    """
    Re-run Pass 2 on rows currently in the review queue because Gemini errored
    or was not configured the first time round.

    Returns: {"retried": N, "auto_applied": N, "still_in_review": N, "aborted_gemini": bool}
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)

    if not gemini_api_key:
        log("Cannot retry — GEMINI_API_KEY is still not set.")
        return {"retried": 0, "auto_applied": 0, "still_in_review": 0, "aborted_gemini": True}

    master_path = _pick_master(project_dir, region_code)
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    by_uid = {row["Unique ID"]: row for _, row in master.iterrows()}

    queue = db.get_s7_review_queue(project_id)
    failed = [q for q in queue if (q.get("reason") or "").startswith(("Gemini error", "Gemini not configured"))]
    if not failed:
        log("No Gemini-failed rows in the review queue.")
        return {"retried": 0, "auto_applied": 0, "still_in_review": 0, "aborted_gemini": False}

    log(f"Retrying Gemini on {len(failed):,} failed row(s)")

    candidates = []
    for q in failed:
        uid = q["unique_id"]
        row = by_uid.get(uid)
        if row is None:
            continue
        candidates.append({
            "unique_id": uid,
            "officer_name": str(row.get("Officer name", "")),
            "company_name": str(row.get("Company Name", "")),
            "re_name": q.get("re_name", ""),
            "_match_type": q.get("match_type") or "person",
        })

    auto, review, _errors, aborted = _run_gemini_pass2(
        project_id, candidates, gemini_api_key, db, log, cancel_event,
    )

    return {"retried": len(candidates), "auto_applied": auto,
            "still_in_review": review, "aborted_gemini": aborted}


def apply_re_decisions_and_save(project_id, project_dir, region_code, db):
    """
    Apply all Stage 7 decisions to the master and save master_{REGION}_re_flagged.csv.
    """
    master_path = _pick_master(project_dir, region_code)
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)

    decisions = db.get_s7_decisions(project_id)

    def get_re_match(uid):
        d = decisions.get(uid)
        if not d or d["label"] != "match":
            return "N"
        return "Y"

    def get_potential(uid):
        d = decisions.get(uid)
        if not d or d["label"] != "match":
            return ""
        if d["pass_num"] == 1:
            return "H"
        return "M"

    def get_match_confirmed(uid):
        d = decisions.get(uid)
        if not d or d["label"] != "match":
            return ""
        if d["pass_num"] == 1:
            return "confirmed"
        if d["pass_num"] == 2 and (d.get("confidence") or 0) >= PASS2_AUTO:
            return "tentative"
        return "human"

    master["RE Match?"] = master["Unique ID"].apply(get_re_match)
    master["Potential"] = master["Unique ID"].apply(get_potential)
    master["Match?"] = master["Unique ID"].apply(get_match_confirmed)

    out_path = os.path.join(project_dir, f"master_{region_code}_re_flagged.csv")
    master.to_csv(out_path, index=False)
    return len(master)
