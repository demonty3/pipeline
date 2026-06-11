"""
Stage 2 — Regional merge + Unique ID + SIC industry labels.

What it does, in plain English:
  Take every per-postcode CSV from Stage 1, glue them into one regional
  master sheet, and tidy it for the rest of the pipeline:
    - Count each person's directorships across the region
    - Flag who's a duplicate (same Surname + First Name + DOB) so Apollo
      isn't asked to look up the same person twice in Stage 3
    - Assign sequential Unique IDs (#LE1-0001 style) — these are the
      load-bearing keys for every later join
    - Map the numeric SIC codes to readable industry labels

  Output: master_<REGION>_raw.csv in the project folder.

What's different from the old process:
  Replaces Steps 4–5 of the 18-step flow. Used to be: stitch all the
  per-postcode CSVs together by hand, build a VLOOKUP/XLOOKUP against a
  separate SIC sheet, and assign IDs with Excel formulas. The IDs in
  particular are now stable enough to be used as the join key end-to-end —
  in the old process, fuzzy name-matching had to do that job.
"""
import os
import glob
import re
import json
import pandas as pd
from data.sic_codes import SIC_LABELS


def backfill_unique_ids(project_dir, region_code, starting_counter, progress_cb=None):
    """
    Backfill empty Unique IDs across every master_<RC>_*.csv in a project.

    Stages 2-7 never re-order or filter the master row-wise (Stage 4 ingest
    joins by UID/name into the existing row; Stages 5/7 add columns; Stage 6
    merges by UID), so a row-index-based ID assignment stays consistent across
    files. Reads the row count from master_<RC>_raw.csv, generates IDs once,
    then applies the same list to every other master file with matching row
    count. Idempotent: rows that already have a Unique ID keep it.

    Returns (n_files_updated, max_counter).
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)

    raw_path = os.path.join(project_dir, f"master_{region_code}_raw.csv")
    if not os.path.exists(raw_path):
        log(f"  No master_{region_code}_raw.csv — nothing to backfill")
        return 0, starting_counter

    raw = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
    if "Unique ID" not in raw.columns:
        raw.insert(0, "Unique ID", "")

    # Build the canonical row → UID list. Preserve any existing IDs; fill gaps
    # using the counter, never reusing numbers from existing IDs.
    counter = starting_counter
    for existing in raw["Unique ID"].astype(str):
        m = re.search(r"(\d+)$", existing.strip())
        if m:
            counter = max(counter, int(m.group(1)))

    uid_map = []
    filled = 0
    for existing in raw["Unique ID"].astype(str):
        existing = existing.strip()
        if existing:
            uid_map.append(existing)
        else:
            counter += 1
            uid_map.append(f"#{region_code}-{counter:04d}")
            filled += 1

    if filled == 0:
        log("  All Unique IDs already populated — nothing to backfill")
        return 0, counter

    log(f"  Backfilling {filled:,} missing Unique IDs (counter → {counter})")

    # Apply to every master file in the project folder.
    pattern = os.path.join(project_dir, f"master_{region_code}_*.csv")
    files = sorted(glob.glob(pattern))
    n_rows = len(raw)
    n_updated = 0
    for f in files:
        df = pd.read_csv(f, dtype=str, keep_default_na=False)
        if len(df) != n_rows:
            log(f"  SKIP {os.path.basename(f)}: row count {len(df)} ≠ raw {n_rows}")
            continue
        if "Unique ID" not in df.columns:
            df.insert(0, "Unique ID", uid_map)
        else:
            df["Unique ID"] = uid_map
        df.to_csv(f, index=False)
        n_updated += 1
        log(f"  Updated {os.path.basename(f)}")

    return n_updated, counter


# ── Stable Unique IDs across re-merges ────────────────────────────────────────
# A Unique ID must follow the PERSON, not the row position. Earlier, run_merge
# assigned IDs sequentially by row order, so a re-fetch that changed the row
# count renumbered everyone — which silently mis-joined Apollo enrichment (keyed
# on old IDs) onto the wrong people. We now persist an identity→UID map per
# project and reuse it on every merge.

def _identity_key(surname, first, dob, company_number) -> str:
    """Identity of a (person, company) row — stable across re-fetches because
    Companies House returns the same Surname / DOB / Company Number for the same
    appointment. This is the key a Unique ID is pinned to."""
    return "|".join(str(x).strip().lower()
                    for x in (surname, first, dob, company_number))


def _uid_map_path(project_dir, region_code):
    return os.path.join(project_dir, f"uid_map_{region_code}.json")


def _load_uid_map(project_dir, region_code):
    """Returns (identity→UID dict, high-water counter)."""
    p = _uid_map_path(project_dir, region_code)
    if os.path.exists(p):
        with open(p) as fh:
            d = json.load(fh)
        return dict(d.get("map", {})), int(d.get("counter", 0))
    return {}, 0


def _save_uid_map(project_dir, region_code, uid_map, counter):
    with open(_uid_map_path(project_dir, region_code), "w") as fh:
        json.dump({"counter": counter, "map": uid_map}, fh)


def count_missing_unique_ids(project_dir, region_code):
    """
    Quick scan: how many rows in master_<RC>_raw.csv have an empty Unique ID?
    Powers the project-page banner offering the one-click repair.
    """
    raw_path = os.path.join(project_dir, f"master_{region_code}_raw.csv")
    if not os.path.exists(raw_path):
        return 0
    df = pd.read_csv(raw_path, dtype=str, keep_default_na=False, usecols=lambda c: c == "Unique ID")
    if "Unique ID" not in df.columns:
        return -1  # column missing entirely — sentinel
    return int((df["Unique ID"].astype(str).str.strip() == "").sum())


def run_merge(project_dir, region_code, id_prefix, starting_counter, progress_cb=None):
    """
    Args:
        project_dir: absolute path to this project's folder
        region_code: e.g. "LE1"
        id_prefix: used in Unique ID, e.g. "LE1" → "#LE1-0001"
        starting_counter: int, the last-used counter value (0 for a fresh project)
        progress_cb: optional callable(str)

    Returns:
        (row_count: int, new_counter: int)
    """

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    postcodes_dir = os.path.join(project_dir, "postcodes")
    csv_files = sorted(glob.glob(os.path.join(postcodes_dir, "results_*.csv")))

    if not csv_files:
        raise ValueError(f"No results_*.csv files found in {postcodes_dir}")

    # ── Load and concatenate ──────────────────────────────────────────────────
    log(f"Loading {len(csv_files)} postcode file(s)...")
    dfs = []
    for path in csv_files:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        dfs.append(df)

    master = pd.concat(dfs, ignore_index=True)
    log(f"  {len(master):,} rows loaded before dedup check")

    # ── Directorships count ───────────────────────────────────────────────────
    # Count how many distinct company-postcode pairs each person appears in
    # across all fetched areas. Stage 1 already deduped within-postcode, so
    # each row here represents one distinct company assignment.
    name_key = (
        master["Surname"].str.strip().str.lower()
        + "|"
        + master["First Name"].str.strip().str.lower()
    )
    directorship_counts = name_key.value_counts()
    master["Directorships"] = name_key.map(directorship_counts).astype(int)
    log(f"  Directorships counted (max: {master['Directorships'].max()})")

    # ── Dedup flag ────────────────────────────────────────────────────────────
    # We flag duplicates but keep all rows in the master.
    # Stage 3 (Apollo magazine) will build the upload list from non-duplicates only.
    dedup_key = (
        master["Surname"].str.strip().str.lower()
        + "|"
        + master["First Name"].str.strip().str.lower()
        + "|"
        + master["Officer date of birth"].str.strip()
    )
    master["Apollo Duplicate"] = dedup_key.duplicated(keep="first")
    dup_count = master["Apollo Duplicate"].sum()
    log(f"  {dup_count:,} duplicate persons flagged ({len(master) - dup_count:,} unique for Apollo upload)")

    # ── Unique IDs (stable across re-merges) ──────────────────────────────────
    # Assign by IDENTITY, not row order: a person who already has a UID keeps it,
    # only genuinely new rows mint a fresh one. This is what stops a re-fetch from
    # renumbering everyone and mis-joining enrichment onto the wrong people.
    uid_map, persisted_counter = _load_uid_map(project_dir, region_code)
    counter = max(starting_counter, persisted_counter)

    # Migration: an existing project has IDs in its current raw master but no map
    # yet. Seed the map from it so the first post-fix merge PRESERVES today's IDs
    # rather than re-minting them.
    raw_path_existing = os.path.join(project_dir, f"master_{region_code}_raw.csv")
    if not uid_map and os.path.exists(raw_path_existing):
        prev = pd.read_csv(raw_path_existing, dtype=str, keep_default_na=False)
        if "Unique ID" in prev.columns:
            for _, r in prev.iterrows():
                uid = str(r.get("Unique ID", "")).strip()
                if not uid:
                    continue
                k = _identity_key(r.get("Surname", ""), r.get("First Name", ""),
                                  r.get("Officer date of birth", ""), r.get("Company Number", ""))
                uid_map.setdefault(k, uid)
                m = re.search(r"(\d+)$", uid)
                if m:
                    counter = max(counter, int(m.group(1)))
            log(f"  Seeded UID map from existing master: {len(uid_map):,} known people")

    used = set()
    uid_list = []
    reused = minted = 0
    for _, r in master.iterrows():
        k = _identity_key(r.get("Surname", ""), r.get("First Name", ""),
                          r.get("Officer date of birth", ""), r.get("Company Number", ""))
        uid = uid_map.get(k)
        if uid is not None and uid not in used:
            reused += 1
        else:
            # New person, OR a genuine duplicate row of one already placed this
            # merge — mint a fresh ID so every row keeps a UNIQUE Unique ID.
            counter += 1
            uid = f"#{id_prefix}-{counter:04d}"
            uid_map.setdefault(k, uid)
            minted += 1
        used.add(uid)
        uid_list.append(uid)
    master["Unique ID"] = uid_list
    _save_uid_map(project_dir, region_code, uid_map, counter)
    log(f"  Unique IDs: {reused:,} reused, {minted:,} newly minted (counter → {counter})")

    # ── SIC label ─────────────────────────────────────────────────────────────
    def _map_sic(sic_str):
        if not sic_str:
            return ""
        codes = [c.strip() for c in str(sic_str).split(",")]
        labels = []
        for c in codes:
            if not c or not c.isdigit() or not (4 <= len(c) <= 5):
                continue  # skip blanks, non-numeric, and malformed codes
            # CH API returns 5-digit codes; old v4 data has 4-digit codes.
            # Try as-is, then zero-padded to 5 digits.
            label = SIC_LABELS.get(c) or SIC_LABELS.get(c.zfill(5)) or f"Unknown ({c})"
            labels.append(label)
        return "; ".join(labels)

    master["SIC Industry"] = master["Company SIC codes"].apply(_map_sic)
    log("  SIC labels mapped")

    # ── Column order ──────────────────────────────────────────────────────────
    # Unique ID first, then the 26 CH cols, then our two internal cols.
    ch_cols = [
        "Surname", "First Name", "Middle Names", "Officer name",
        "Officer occupation", "Officer role", "Officer nationality",
        "Officer date of birth", "Officer address line one",
        "Officer address locality", "Officer address country",
        "Officer address post code", "Officer country of residence",
        "Officer appointment date", "Appointment", "Officer resignation date",
        "Company Name", "Company Number", "Company Status", "Company Type",
        "Company date of creation", "Company address line one",
        "Company address locality", "Company address country",
        "Company address post code", "Company SIC codes",
    ]
    ordered = ["Unique ID"] + ch_cols + ["SIC Industry", "Directorships", "Apollo Duplicate"]
    # Keep any extra columns that might have crept in rather than silently dropping
    extra = [c for c in master.columns if c not in ordered]
    master = master[ordered + extra]

    # ── Save ──────────────────────────────────────────────────────────────────
    output_path = os.path.join(project_dir, f"master_{region_code}_raw.csv")
    master.to_csv(output_path, index=False)
    log(f"  Saved: {output_path} ({len(master):,} rows)")

    return len(master), counter
