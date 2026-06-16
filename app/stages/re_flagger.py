"""
Stage 7 — Raiser's Edge match tiering (mark which people are existing donors).

What it does, in plain English:
  The operator uploads the Raiser's Edge export. The RE Name column mixes
  person and company names in the same field, which is the bit that makes
  this stage hard. Matching on the name alone produces false positives on
  common names ("John Smith"), so we corroborate a name match with every
  identifying factor the RE export carries — email, postcode, town. We:
    - Normalise both sides (strip titles like "Mr"/"Dr" and suffixes
      like "Jr"/"II", lowercase, collapse whitespace; company names also
      get Charles's Ltd/PLC/UK cleanup so suffixes don't drag the score)
    - Block by shared name token (plus an exact-email index, so a donor
      whose name changed still surfaces) so we only score plausible
      pairs, not the full master × RE cross-product
    - Score the name with rapidfuzz token_sort_ratio, then place each
      row in a certainty tier (Match > Probable > Potential) — formulas
      only, no LLM. The rules live in _tier() below; the normative spec
      is cchq-orchestrator/references/schema_contract.md.
  Adds RE Match? / Potential / Match? columns to the master and saves
  master_<REGION>_re_flagged.csv. The per-decision reason records which
  factors fired, for audit.

  RE data is highly sensitive donor data and MUST NOT be sent to any LLM
  (Charles, 2026-06-10) — every decision here is deterministic.

What's different from the old process:
  Replaces Steps 13–14. Used to be: by-eye XLOOKUP of the RE name list
  against the master, with no way to tell person from company entries.
  Charles called this out explicitly as the stage most worth improving.
"""
import os
import re
import pandas as pd
from rapidfuzz import fuzz

from stages.text_cleanup import clean_company_name

# Name-similarity thresholds (rapidfuzz token_sort_ratio, 0–100).
NAME_STRONG = 90   # near-certain name match
NAME_MED    = 78   # plausible name match — the floor for any flag at all

# Blocking: skip RE name tokens this common — they're effectively stopwords
# ("construction", "services") and would balloon the candidate set.
TOKEN_DOC_CAP = 60
MIN_TOKEN_LEN = 3

TITLE_PREFIXES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "lady", "lord", "rev", "the"}
NAME_SUFFIXES  = {"jr", "sr", "ii", "iii", "iv", "esq"}

# Tier label → value shown in the master's "Potential" column.
TIER_DISPLAY = {"match": "Match", "probable": "Probable", "potential": "Potential"}
# Tier label → certainty order, for picking the best RE candidate per row.
TIER_ORDER = {"match": 3, "probable": 2, "potential": 1, None: 0}

# Master-side email columns, strongest-evidence first. Apollo Email comes from
# Stage 4 enrichment; EmailAddress is folded in by a Stage 6b VoteSource return
# (both keyed on Unique ID, so each is an email for the row's own person).
EMAIL_COLS = ["Apollo Email", "EmailAddress"]


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
    """UK outward code: 'CM1 2AB' / 'CM12AB' → 'CM1'. '' if blank.

    Handles outward-only values too ('LE12', 'SW1A' → unchanged): the inward
    part is always digit+2letters, so blind last-3 stripping would mangle a
    4-char outward-only postcode down to a single letter and silently kill
    postcode corroboration for that record.
    """
    s = re.sub(r"\s+", "", str(postcode or "")).upper()
    if not s:
        return ""
    if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}", s):  # full postcode
        return s[:-3]
    if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", s):            # outward only
        return s
    return s[:-3] if len(s) > 3 else s                       # malformed: legacy rule


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
    are optional — matching degrades gracefully to name-only when absent
    (everything then caps at the Potential tier).

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


def _tier(name_score, email_match, pc_match, city_match):
    """
    The certainty formula. Returns "match" / "probable" / "potential" / None.

    Charles's rules (2026-06-10): a name-only hit is never more than
    Potential; an exact email elevates to Match; geographic corroboration
    earns the middle tier. The email+weak-name case lands on Probable, not
    Match, because shared company mailboxes (info@…) can collide across
    different officers of the same firm.
    """
    if email_match and name_score >= NAME_MED:
        return "match"
    if email_match:
        return "probable"
    if name_score >= NAME_STRONG and (pc_match or city_match):
        return "probable"
    if name_score >= NAME_MED:
        return "potential"
    return None


def run_re_flagging(project_id, project_dir, region_code, re_path,
                    progress_cb=None, db=None, cancel_event=None):
    """
    Run RE match tiering — a single deterministic pass, no LLM.

    Returns summary dict: {match, probable, potential, no_flag}
    """

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    re_records = _load_re_records(re_path)
    log(f"RE export loaded: {len(re_records):,} records")
    have_pc   = sum(1 for r in re_records if r["postcode_out"])
    have_mail = sum(1 for r in re_records if r["email"])
    log(f"  corroborating factors available — postcode: {have_pc:,}, email: {have_mail:,}"
        + ("  (name-only export — everything caps at Potential)" if not (have_pc or have_mail) else ""))

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
    email_cols = [c for c in EMAIL_COLS if c in master.columns]

    # ── Inverted token index over RE records, for blocking ────────────────────
    # Comparing every master row against every RE record is O(N·M) and was the
    # old bottleneck. Instead we only score RE records that share a name token
    # with the master row; tokens that are too common are skipped as stopwords.
    # An exact-email index runs alongside it so a same-email donor whose name
    # shares no token (married name, nickname) still gets scored.
    token_index = {}
    email_index = {}
    for i, r in enumerate(re_records):
        for t in r["tokens"]:
            token_index.setdefault(t, []).append(i)
        if r["email"]:
            email_index.setdefault(r["email"], []).append(i)
    common = {t for t, ids in token_index.items() if len(ids) > TOKEN_DOC_CAP}
    if common:
        sample = ", ".join(sorted(common)[:5])
        log(f"  {len(common)} common token(s) skipped for blocking (e.g. {sample})")

    counts = {"match": 0, "probable": 0, "potential": 0}
    no_flag = 0
    decisions = []  # (uid, re_name, match_type, label, confidence, reason)
    cancelled = False

    for idx in range(len(master)):
        if cancel_event and cancel_event.is_set():
            log(f"  Cancelled at row {idx:,}/{len(master):,} — previous decisions left untouched")
            cancelled = True
            break

        uid = master.at[idx, "Unique ID"]
        person_norm  = _normalise(f"{master.at[idx, 'Surname']} {master.at[idx, 'First Name']}")
        company_norm = _norm_company(master.at[idx, "Company Name"])

        # master-side identifying factors (officer or company)
        m_pc = {_outward(master.at[idx, "Officer address post code"]),
                _outward(master.at[idx, "Company address post code"])} - {""}
        m_city = {_simple(master.at[idx, "Officer address locality"]),
                  _simple(master.at[idx, "Company address locality"])} - {""}
        m_emails = {_simple(master.at[idx, c]) for c in email_cols} - {""}

        # candidate RE records: those sharing a (non-stopword) name token,
        # plus any exact email hit regardless of name
        cand_ids = set()
        row_tokens = {t for t in set(person_norm.split()) | set(company_norm.split())
                      if len(t) >= MIN_TOKEN_LEN}
        for t in row_tokens:
            if t not in common:
                cand_ids.update(token_index.get(t, ()))
        for e in m_emails:
            cand_ids.update(email_index.get(e, ()))
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

            email_match = bool(r["email"] and r["email"] in m_emails)
            pc_match    = bool(r["postcode_out"] and r["postcode_out"] in m_pc)
            city_match  = bool(r["city"] and r["city"] in m_city)

            # Pick the best RE hit by TIER first, then corroboration-weighted
            # rank as the tie-break within a tier. Rank alone is not monotone
            # with the tier formula — e.g. an unrelated exact-name stranger
            # (rank 100, Potential) would outrank the actual donor found via
            # exact email with a changed name (rank ~85, Probable) and the
            # email evidence would be silently discarded.
            label = _tier(name_score, email_match, pc_match, city_match)
            rank = name_score + (40 if email_match else 0) + (15 if pc_match else 0) + (6 if city_match else 0)
            key = (TIER_ORDER[label], rank)
            if best is None or key > best["key"]:
                best = {"key": key, "label": label, "name_score": name_score,
                        "match_type": match_type, "email_match": email_match,
                        "pc_match": pc_match, "city_match": city_match,
                        "re_name": r["name"], "re_postcode": r["postcode_out"]}

        if best is None or best["label"] is None:
            no_flag += 1
        else:
            ns = best["name_score"]
            factors = []
            if best["email_match"]: factors.append("email exact")
            if best["pc_match"]:    factors.append(f"postcode {best['re_postcode']}")
            if best["city_match"]:  factors.append("town")
            factor_str = ", ".join(factors) if factors else "name only"
            decisions.append((uid, best["re_name"], best["match_type"], best["label"],
                              round(ns / 100, 3), f"name {ns:.0f}, {factor_str}"))
            counts[best["label"]] += 1

        if (idx + 1) % 5000 == 0:
            log(f"  Matched {idx + 1:,}/{len(master):,} rows...")

    # Only a COMPLETED run replaces the stored decisions, and it does so in one
    # atomic transaction — a cancelled or crashed run leaves the previous run's
    # audit trail (and its master_*_re_flagged.csv) fully consistent.
    if not cancelled:
        db.replace_s7_decisions(project_id, decisions)
        log(f"Tiering done: {counts['match']:,} Match | {counts['probable']:,} Probable | "
            f"{counts['potential']:,} Potential | {no_flag:,} no flag")

    return {**counts, "no_flag": no_flag, "cancelled": cancelled}


def apply_re_decisions_and_save(project_id, project_dir, region_code, db):
    """
    Apply all Stage 7 decisions to the master and save master_{REGION}_re_flagged.csv.

    Column contract (master-internal; the exporter does the golden mapping):
      RE Match? — Y for every flagged tier, N otherwise; all flagged rows land
                  on the deliverable's "Potential RE Match" tab
      Potential — the certainty tier: Match / Probable / Potential
      RE Name   — the matched RE donor's name (golden's "Potential" column
                  showed this; the exporter maps it back there)
      Match?    — internal audit trail: which factors fired (overwritten by the
                  sanity check at export, never shown to the Treasurers)
    """
    master_path = _pick_master(project_dir, region_code)
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)

    decisions = db.get_s7_decisions(project_id)

    def get_re_match(uid):
        d = decisions.get(uid)
        return "Y" if d and d["label"] in TIER_DISPLAY else "N"

    def get_potential(uid):
        d = decisions.get(uid)
        return TIER_DISPLAY.get(d["label"], "") if d else ""

    def get_factors(uid):
        d = decisions.get(uid)
        if not d or d["label"] not in TIER_DISPLAY:
            return ""
        return d.get("reason") or ""

    def get_re_name(uid):
        d = decisions.get(uid)
        if not d or d["label"] not in TIER_DISPLAY:
            return ""
        return d.get("re_name") or ""

    master["RE Match?"] = master["Unique ID"].apply(get_re_match)
    master["Potential"] = master["Unique ID"].apply(get_potential)
    master["RE Name"] = master["Unique ID"].apply(get_re_name)
    master["Match?"] = master["Unique ID"].apply(get_factors)

    out_path = os.path.join(project_dir, f"master_{region_code}_re_flagged.csv")
    master.to_csv(out_path, index=False)
    return len(master)
