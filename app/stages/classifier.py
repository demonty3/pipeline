"""
Stage 5 — Y/T/N sanity check (does Apollo's match really refer to this person?).

What it does, in plain English:
  For every Apollo-matched row, decide whether the Apollo contact really is
  the Companies House officer. Fully deterministic three-tier cascade:
    Pass 1: string similarity (rapidfuzz token-sort ratio)
            ≥85 → Yes, <45 → No, everything in between → Tentative
    Pass 2: evidence pass on the Tentative band — nickname-normalised
            re-score (Tom↔Thomas, Andy↔Andrew…) and email corroboration
            (the Apollo email's local part contains the officer's surname).
            Positive evidence upgrades T → Y; no evidence leaves the row T.
    Pass 3: rows still Tentative reach the operator's web UI
  Every decision is written to the stage5_decisions DB table and to
  classifications_log.csv for audit.

What's different from the old process:
  Replaces Steps 9 and 15. Used to be: every Apollo-matched row got a
  human Y/T/N decision — ~7,200 rows for Leicester. With this cascade
  the operator only sees the genuinely ambiguous residue; the obvious
  Yes and No are auto-resolved and the evidence pass clears the
  nickname/email cases. (An earlier version used Gemini for Pass 2;
  removed 2026-06-11 — quota stalls made it unreliable, and the evidence
  pass resolves the same band deterministically.)
"""
import os
import csv
import re
import pandas as pd
from rapidfuzz import fuzz

# Score thresholds
PASS1_YES = 85    # token_sort_ratio >= this → Y
PASS1_NO  = 45    # token_sort_ratio < this  → N (middle band → Tentative for Pass 2)

# Placeholder strings Apollo writes when it couldn't enrich a row. Treated as
# empty when building the Apollo name, so a row with "N/A N/A" lands in
# Pass-1 auto-N (can't outreach without contact info) instead of fuzzy-matching
# into the Tentative band and clogging the human review queue.
APOLLO_NULL = {"", "n/a", "na", "n\\a", "-", "—", "none", "null", "."}

# Common UK first-name variants, mapped to one canonical form. Both names are
# canonicalised before the Pass-2 re-score, so "Tom Carson" vs "THOMAS CARSON"
# scores like an exact match. Deliberately modest — only unambiguous pairs.
NICKNAMES = {
    "tom": "thomas", "tommy": "thomas",
    "andy": "andrew", "drew": "andrew",
    "mike": "michael", "mick": "michael",
    "bernie": "bernard",
    "frank": "francis", "fran": "francis",
    "bob": "robert", "rob": "robert", "bobby": "robert", "robbie": "robert",
    "bill": "william", "billy": "william", "will": "william",
    "dave": "david",
    "steve": "stephen", "steven": "stephen",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "liz": "elizabeth", "beth": "elizabeth", "lizzie": "elizabeth",
    "kate": "katherine", "cathy": "katherine", "katie": "katherine",
    "catherine": "katherine", "kathryn": "katherine",
    "sue": "susan", "susie": "susan",
    "tony": "anthony",
    "nick": "nicholas",
    "chris": "christopher",
    "dan": "daniel", "danny": "daniel",
    "matt": "matthew",
    "joe": "joseph", "joey": "joseph",
    "sam": "samuel",
    "ben": "benjamin",
    "ed": "edward", "eddie": "edward", "ted": "edward",
    "pete": "peter",
    "dick": "richard", "rick": "richard", "richie": "richard",
    "greg": "gregory",
    "jen": "jennifer", "jenny": "jennifer",
    "becky": "rebecca",
    "vicky": "victoria", "vicki": "victoria",
    "pat": "patrick",
    "charlie": "charles", "chuck": "charles",
    "harry": "henry",
    "ron": "ronald", "ronnie": "ronald",
    "don": "donald",
    "ken": "kenneth", "kenny": "kenneth",
    "ray": "raymond",
    "phil": "philip", "phillip": "philip",
    "gerry": "gerald", "jerry": "gerald",
    "terry": "terence",
    "doug": "douglas",
    "stan": "stanley",
    "alex": "alexander",
    "fred": "frederick", "freddie": "frederick",
    "geoff": "geoffrey", "jeff": "geoffrey",
}

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


def run_passes_1_and_2(project_id, project_dir, region_code, progress_cb=None, db=None, cancel_event=None):
    """
    Run Pass 1 (fuzzy score) and Pass 2 (deterministic evidence) of the
    classifier. Writes decisions to the DB and classifications_log.csv.
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
                # Evidence-pass inputs:
                "surname": str(row.get("Surname", "")).strip(),
                "first_name": str(row.get("First Name", "")).strip(),
                "apollo_email": str(row.get("Apollo Email", "")).strip(),
            })
            pass1_t += 1

    _append_log(log_path, log_entries)
    log(f"Pass 1 done: {pass1_y:,} Y | {pass1_n:,} N | {pass1_t:,} Tentative → Pass 2")

    # ── Pass 2: deterministic evidence ────────────────────────────────────────
    if not tentative_rows:
        log("No Tentative rows — skipping evidence pass")
        return {"pass1_y": pass1_y, "pass1_n": pass1_n, "pass1_t": pass1_t,
                "pass2_y": 0, "pass2_review": 0}

    pass2_y, pass2_review = _evidence_pass2(
        project_id, tentative_rows, log_path, db, log,
    )

    return {
        "pass1_y": pass1_y, "pass1_n": pass1_n, "pass1_t": pass1_t,
        "pass2_y": pass2_y, "pass2_review": pass2_review,
    }


def _canonical_first_names(name):
    """Map every token of a name through the NICKNAMES table."""
    tokens = _normalise(name).split()
    return " ".join(NICKNAMES.get(t, t) for t in tokens)


def _email_corroborates(surname, first_name, email):
    """
    True if the Apollo email's local part contains the officer's surname
    (≥4 letters, to avoid short-surname false hits like "Li") or the
    officer's first-initial + surname (e.g. hwgsmith@ for Henry Smith —
    accepted at any surname length because the initial pins it down).
    """
    if not email or "@" not in email:
        return False
    local = re.sub(r"[^a-z]", "", email.split("@", 1)[0].lower())
    sn = re.sub(r"[^a-z]", "", (surname or "").lower())
    if not sn:
        return False
    if len(sn) >= 4 and sn in local:
        return True
    fi = re.sub(r"[^a-z]", "", (first_name or "").lower())[:1]
    return bool(fi) and (fi + sn) in local


def _evidence_pass2(project_id, rows, log_path, db, log):
    """
    Deterministic Pass 2 over the Tentative band. Replaces the old Gemini pass
    (removed 2026-06-11). Two checks, in order; positive evidence upgrades the
    row to Y, otherwise it STAYS T and goes to the human review queue. The pass
    never downgrades to N — absence of evidence is not evidence of mismatch.

    Returns (pass2_y, pass2_review).
    """
    pass2_y = pass2_review = 0
    log_entries = []

    log(f"Pass 2 (evidence): {len(rows):,} Tentative row(s)")

    for r in rows:
        uid = r["unique_id"]
        label, confidence, reason = "T", 0.0, "No deterministic evidence — human review"

        # 1. Nickname-normalised re-score: Tom Carson vs THOMAS CARSON.
        score = fuzz.token_sort_ratio(_canonical_first_names(r["officer_name"]),
                                      _canonical_first_names(r["apollo_name"]))
        if score >= PASS1_YES:
            label, confidence = "Y", score / 100
            reason = f"Nickname-normalised score {score}"
        # 2. Email corroboration: david.bennett@… for officer David Bennett.
        elif _email_corroborates(r.get("surname", ""), r.get("first_name", ""),
                                 r.get("apollo_email", "")):
            label, confidence = "Y", 0.9
            reason = "Email corroborates officer name"

        db.log_s5_decision(project_id, uid, 2, label, confidence=confidence, reason=reason)
        log_entries.append({
            "unique_id": uid, "pass_num": 2, "label": label,
            "confidence": confidence, "reason": reason,
            "officer_name": r["officer_name"], "apollo_name": r["apollo_name"],
        })
        if label == "Y":
            pass2_y += 1
        else:
            pass2_review += 1

    _append_log(log_path, log_entries)
    log(f"Pass 2 done: {pass2_y:,} upgraded to Y | {pass2_review:,} to review")
    return pass2_y, pass2_review


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
