"""
Stage 7 — Raiser's Edge fuzzy flagger (mark which people are existing donors).

What it does, in plain English:
  The operator uploads the Raiser's Edge export. The RE Name column mixes
  person and company names in the same field, which is the bit that makes
  this stage hard. Matching on the name alone produces false positives on
  common names ("John Smith"), so we now fold in every identifying factor
  the RE export carries — postcode, town, email — to corroborate a name
  match before flagging it. We:
    - Normalise both sides (strip titles like "Mr"/"Dr" and suffixes
      like "Jr"/"II", lowercase, collapse whitespace; company names also
      get Charles's Ltd/PLC/UK cleanup so suffixes don't drag the score)
    - Block by shared name token so we only score plausible pairs, not
      the full master × RE cross-product
    - Score the name with rapidfuzz token_sort_ratio, then corroborate
      with exact email / matching postcode / matching town
    - Run the deterministic-first cascade:
        decisive  (email match, or strong name + postcode)  → auto-flag (H)
        ambiguous (strong name w/o corroboration, or medium  → Gemini Flash;
                   name) — Gemini sees the factors too           ≥0.80 auto,
                                                                  else review
        weak      (low name and no email)                   → don't flag
  Adds RE Match? / Potential / Match? columns to the master and saves
  master_<REGION>_re_flagged.csv. The per-decision reason records which
  factors fired, for the review screen and audit.

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

from stages.text_cleanup import clean_company_name

# Name-similarity thresholds (rapidfuzz token_sort_ratio, 0–100).
NAME_STRONG = 90   # near-certain name match
NAME_MED    = 78   # plausible name match — worth corroborating / asking Gemini

PASS2_AUTO  = 0.80 # Gemini confidence → auto-apply
PASS2_BATCH = 20

# Blocking: skip RE name tokens this common — they're effectively stopwords
# ("construction", "services") and would balloon the candidate set.
TOKEN_DOC_CAP = 60
MIN_TOKEN_LEN = 3

GEMINI_MODEL = "gemini-2.5-flash"

TITLE_PREFIXES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "lady", "lord", "rev", "the"}
NAME_SUFFIXES  = {"jr", "sr", "ii", "iii", "iv", "esq"}

SYSTEM_PROMPT = """You are a data-quality assistant for a UK charity donor database (Raiser's Edge).
Decide whether a Raiser's Edge constituent record refers to the same person or company as a UK Companies House officer record.

Each item gives the officer name, company name, candidate RE name, and — when available — corroborating signals: whether the officer's postcode and town match the RE record's. A matching postcode is strong corroboration; a name match alone (especially a common name) is weak.

Rules:
- match = strong evidence (close name AND a corroborating postcode/town, or an exact/near-exact full name)
- no_match = different entity, or only a loose/common-name overlap with no corroboration

Respond ONLY with a valid JSON array — no prose, no markdown fences.
Each element: {unique_id, label, match_type, confidence, reason}
  label: "match" or "no_match"
  match_type: "person" or "company"
  confidence: float 0.0–1.0
  reason: one short sentence"""


def _normalise(name):
    """Strip titles, suffixes, punctuation, lowercase."""
    name = str(name or "").lower().strip()
    name = re.sub(r"[^\w\s]", " ", name)
    tokens = [t for t in name.split() if t not in TITLE_PREFIXES and t not in NAME_SUFFIXES]
    return " ".join(tokens)


def _norm_company(name):
    """Company cleaner (Charles's Ltd/PLC/UK logic) + normalise, so entity
    suffixes don't drag the similarity score down."""
    return _normalise(clean_company_name(str(name or "")))


def _outward(postcode):
    """UK outward code: 'CM1 2AB' / 'CM12AB' → 'CM1'. '' if blank."""
    s = re.sub(r"\s+", "", str(postcode or "")).upper()
    if not s:
        return ""
    return s[:-3] if len(s) > 3 else s


def _simple(s):
    """Lowercase, strip, collapse whitespace — for town / generic compares."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


# RE export column headers we look for (case-insensitive), with fallbacks.
RE_NAME_COLS = ["Name"]
RE_PC_COLS   = ["Postcode", "Post code", "Postal code"]
RE_CITY_COLS = ["City", "Town"]
RE_MAIL_COLS = ["Email address", "Email"]


def _find_col(df, candidates):
    lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _load_re_records(re_path):
    """
    Read the RE export (XLSX/ODS) into a list of records carrying every
    identifying factor we can match on. Name is required; postcode/city/email
    are optional — matching degrades gracefully to name-only when absent.

    Each record: {name, norm, tokens, postcode_out, city, email}
    """
    ext = os.path.splitext(re_path)[1].lower()
    try:
        if ext == ".ods":
            df = pd.read_excel(re_path, engine="odf", dtype=str, keep_default_na=False)
        elif ext == ".csv":
            df = pd.read_csv(re_path, dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(re_path, dtype=str, keep_default_na=False)
    except Exception as e:
        raise ValueError(f"Could not read RE export: {e}")

    name_col = _find_col(df, RE_NAME_COLS)
    if name_col is None and len(df.columns) >= 4:
        name_col = df.columns[3]   # legacy single-list export: Name is column 4
    if name_col is None:
        raise ValueError("Could not find a Name column in the RE export.")

    pc_col   = _find_col(df, RE_PC_COLS)
    city_col = _find_col(df, RE_CITY_COLS)
    mail_col = _find_col(df, RE_MAIL_COLS)

    records = []
    for _, row in df.iterrows():
        raw = str(row[name_col]).strip()
        if not raw:
            continue
        norm = _normalise(raw)
        records.append({
            "name": raw,
            "norm": norm,
            "tokens": {t for t in norm.split() if len(t) >= MIN_TOKEN_LEN},
            "postcode_out": _outward(row[pc_col]) if pc_col else "",
            "city": _simple(row[city_col]) if city_col else "",
            "email": _simple(row[mail_col]) if mail_col else "",
        })
    return records


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

    re_records = _load_re_records(re_path)
    log(f"RE export loaded: {len(re_records):,} records")
    have_pc   = sum(1 for r in re_records if r["postcode_out"])
    have_mail = sum(1 for r in re_records if r["email"])
    log(f"  corroborating factors available — postcode: {have_pc:,}, email: {have_mail:,}"
        + ("  (name-only export — corroboration limited)" if not (have_pc or have_mail) else ""))
    # When the RE export carries NO corroborating factors at all, email/postcode
    # match can never fire — so fall back to auto-flagging on a strong name alone,
    # else even exact full-name donor matches would be demoted to the review tier.
    re_name_only = not (have_pc or have_mail)

    master_path = _pick_master(project_dir, region_code)
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    log(f"Master loaded: {len(master):,} rows from {os.path.basename(master_path)}")
    # Defensive: the corroboration columns below are indexed unconditionally with
    # master.at[idx, ...] — ensure they exist so a master variant missing one
    # (e.g. an imported file) doesn't KeyError mid-loop.
    for _c in ("Officer address post code", "Company address post code",
               "Officer address locality", "Company address locality", "Officer name"):
        if _c not in master.columns:
            master[_c] = ""
    has_apollo_email = "Apollo Email" in master.columns

    # ── Inverted token index over RE records, for blocking ────────────────────
    # Comparing every master row against every RE record is O(N·M) and was the
    # old bottleneck. Instead we only score RE records that share a name token
    # with the master row; tokens that are too common are skipped as stopwords.
    token_index = {}
    for i, r in enumerate(re_records):
        for t in r["tokens"]:
            token_index.setdefault(t, []).append(i)
    common = {t for t, ids in token_index.items() if len(ids) > TOKEN_DOC_CAP}
    if common:
        sample = ", ".join(sorted(common)[:5])
        log(f"  {len(common)} common token(s) skipped for blocking (e.g. {sample})")

    auto_flagged = 0
    gemini_candidates = []  # rows to send to Gemini
    no_flag = 0

    for idx in range(len(master)):
        uid = master.at[idx, "Unique ID"]
        person_norm  = _normalise(f"{master.at[idx, 'Surname']} {master.at[idx, 'First Name']}")
        company_norm = _norm_company(master.at[idx, "Company Name"])

        # master-side identifying factors (officer or company)
        m_pc = {_outward(master.at[idx, "Officer address post code"]),
                _outward(master.at[idx, "Company address post code"])} - {""}
        m_city = {_simple(master.at[idx, "Officer address locality"]),
                  _simple(master.at[idx, "Company address locality"])} - {""}
        m_email = _simple(master.at[idx, "Apollo Email"]) if has_apollo_email else ""

        # candidate RE records: those sharing a (non-stopword) name token
        cand_ids = set()
        row_tokens = {t for t in set(person_norm.split()) | set(company_norm.split())
                      if len(t) >= MIN_TOKEN_LEN}
        for t in row_tokens:
            if t not in common:
                cand_ids.update(token_index.get(t, ()))
        # Fallback: if every token was a stopword (a very common surname with no
        # other distinguishing token), don't silently drop the row — score it
        # against the common-token candidates too rather than never flagging it.
        if not cand_ids:
            for t in row_tokens:
                cand_ids.update(token_index.get(t, ()))

        best = None
        for i in cand_ids:
            r = re_records[i]
            p_score = fuzz.token_sort_ratio(person_norm, r["norm"])
            c_score = fuzz.token_sort_ratio(company_norm, r["norm"])
            if p_score >= c_score:
                name_score, match_type = p_score, "person"
            else:
                name_score, match_type = c_score, "company"

            email_match = bool(m_email and r["email"] and m_email == r["email"])
            pc_match    = bool(r["postcode_out"] and r["postcode_out"] in m_pc)
            city_match  = bool(r["city"] and r["city"] in m_city)

            # Corroboration outranks raw name score when picking the best RE hit.
            rank = name_score + (40 if email_match else 0) + (15 if pc_match else 0) + (6 if city_match else 0)
            if best is None or rank > best["rank"]:
                best = {"rank": rank, "name_score": name_score, "match_type": match_type,
                        "email_match": email_match, "pc_match": pc_match, "city_match": city_match,
                        "re_name": r["name"], "re_postcode": r["postcode_out"]}

        if best is None:
            no_flag += 1
            continue

        ns = best["name_score"]
        factors = []
        if best["email_match"]: factors.append("email exact")
        if best["pc_match"]:    factors.append(f"postcode {best['re_postcode']}")
        if best["city_match"]:  factors.append("town")
        factor_str = ", ".join(factors) if factors else "name only"

        # Deterministic-first cascade:
        #   decisive  → auto-flag (Potential H)
        #   ambiguous → Gemini Flash (it sees the same factors)
        #   weak      → no flag
        if best["email_match"] or (ns >= NAME_STRONG and best["pc_match"]) \
                or (re_name_only and ns >= NAME_STRONG):
            db.log_s7_decision(project_id, uid, best["re_name"], best["match_type"],
                               "match", confidence=min(0.99, ns / 100 + 0.1),
                               reason=f"Auto-match — name {ns} + {factor_str}", pass_num=1)
            auto_flagged += 1
        elif ns >= NAME_MED:
            db.log_s7_decision(project_id, uid, best["re_name"], best["match_type"],
                               "tentative", confidence=ns / 100,
                               reason=f"Tentative — name {ns} ({factor_str})", pass_num=1)
            gemini_candidates.append({
                "unique_id": uid,
                "officer_name": master.at[idx, "Officer name"],
                "company_name": master.at[idx, "Company Name"],
                "re_name": best["re_name"],
                "officer_postcode": " / ".join(sorted(m_pc)),
                "re_postcode": best["re_postcode"],
                "postcode_match": best["pc_match"],
                "city_match": best["city_match"],
                "_match_type": best["match_type"],
                "_score": ns,
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
                # Leave as 'tentative' (NOT 'match') — without Gemini we cannot
                # disambiguate, so an un-corroborated common-name collision must
                # NOT be auto-confirmed as RE Match?=Y; it stays for human review.
                db.log_s7_decision(project_id, c["unique_id"], c["re_name"], c["_match_type"],
                                   "tentative", confidence=0.0,
                                   reason="Gemini not configured — left for human review", pass_num=2)
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
                  "company_name": c["company_name"], "re_name": c["re_name"],
                  "officer_postcode": c.get("officer_postcode", ""),
                  "re_postcode": c.get("re_postcode", ""),
                  "postcode_match": c.get("postcode_match", False),
                  "town_match": c.get("city_match", False)}
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
