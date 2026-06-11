"""
Stage 4 — Apollo result ingestion.

What it does, in plain English:
  Apollo enrichment itself is still a human checkpoint — the operator takes
  a batch from the magazine, uploads it into Apollo's web UI, waits, and
  downloads Apollo's enriched CSV. This module does the part AFTER that:
    - Reads the enriched CSV the operator drops in
    - Joins it back to the master by Unique ID first; falls back to
      (Surname, First Name, normalised Company Name) when the file's IDs
      don't match the master's (e.g. when the file came from a different
      project's run because the operator doesn't have Apollo access of
      their own).
    - Prefixes Apollo's 22 columns with "Apollo " so they don't collide
      with Companies House columns of the same name (Company Name etc.)
    - Writes any still-unmatched rows to apollo_orphans_*.csv
    - Saves master_<REGION>_enriched.csv

  Can ingest one batch at a time (ingest_batch) or many at once
  (ingest_multiple). Batch order doesn't matter — joins happen on key,
  not order.

What's different from the old process:
  Replaces Steps 6–8. The upload step happens in Apollo's own UI as before.
  The join, which used to be a manual XLOOKUP, is now a perfect rejoin
  on Unique ID — with name-match as a graceful fallback.
"""
import os
import re
import pandas as pd
from stages.text_cleanup import clean_company_name

# The 22 columns Apollo adds (exact names as Apollo exports them).
APOLLO_RAW_COLS = [
    "First Name", "Last Name", "Title", "Person Linkedin Url",
    "City", "State", "Country", "Email", "Company Name",
    "Website", "Industry", "# Employees", "Annual Revenue", "Total Funding",
    "Company Phone", "Company Linkedin Url", "Company Street", "Company City",
    "Company Postal Code", "Company State", "Company Country", "Company Founded Year",
]

# Internal names used in the master (prefixed to avoid collision).
APOLLO_INTERNAL_COLS = [f"Apollo {c}" for c in APOLLO_RAW_COLS]

# Of Apollo's 22 columns, these describe the PERSON (not the company they were
# enriched against). Stage 3 sends only one row per unique person to Apollo, so
# only that row comes back enriched; we fan THESE fields out to the person's
# other directorship rows. The remaining (company-level) Apollo fields are NOT
# fanned out — each duplicate row is a different company, and copying the
# representative company's data onto it would be misleading.
PERSON_LEVEL_APOLLO = [
    "First Name", "Last Name", "Title", "Person Linkedin Url",
    "City", "State", "Country", "Email",
]


def _fan_out_person_enrichment(master, log):
    """
    Copy person-level Apollo fields from each enriched representative row onto
    the same person's other rows (their other directorships), keyed by
    (Surname, First Name, Officer date of birth) — the exact key Stage 2 used to
    pick which rows to upload. Only blank target cells are filled, so this never
    overwrites real data and is safe to re-run after every batch ingest.

    Returns the number of rows that received fanned-out enrichment.
    """
    person_cols = [f"Apollo {c}" for c in PERSON_LEVEL_APOLLO if f"Apollo {c}" in master.columns]
    needed = {"Surname", "First Name", "Officer date of birth"}
    if not person_cols or not needed <= set(master.columns):
        return 0

    key = (master["Surname"].str.strip().str.lower() + "|"
           + master["First Name"].str.strip().str.lower() + "|"
           + master["Officer date of birth"].str.strip())
    has_data = master[person_cols].apply(lambda r: any(str(v).strip() for v in r), axis=1)

    filled = 0
    for key_val, idxs in master.groupby(key).groups.items():
        idxs = list(idxs)
        if len(idxs) < 2:
            continue
        # Skip blank-DOB groups: the key collapses to "surname|first|" when DOB is
        # absent, which would merge DISTINCT same-name people and fan one person's
        # contact data onto another. Only fan when the DOB component is present.
        if str(key_val).rsplit("|", 1)[-1].strip() == "":
            continue
        donors = [i for i in idxs if has_data[i]]
        if not donors:
            continue
        src = donors[0]
        for i in idxs:
            if i == src:
                continue
            changed = False
            for col in person_cols:
                if not str(master.at[i, col]).strip() and str(master.at[src, col]).strip():
                    master.at[i, col] = master.at[src, col]
                    changed = True
            if changed:
                filled += 1
    if filled:
        log(f"  Fanned out person enrichment to {filled:,} duplicate-person row(s)")
    return filled


def _name_key(surname: str, first_name: str, company_name: str) -> str:
    """
    Normalised (surname, first name, company name) lookup key for the
    Apollo name-match fallback. Company names go through the same
    cleaning step Stage 3 applies, so a master row's raw CH name
    ("H. WILLIAMSON & SONS LIMITED") matches an Apollo file's already-
    cleaned name ("H. Williamson & Sons") after both are run through
    clean_company_name() + lower().
    """
    s = (surname or "").strip().lower()
    f = (first_name or "").strip().lower()
    c = clean_company_name(company_name or "").lower()
    return f"{s}|{f}|{c}"


def _build_name_index(master: pd.DataFrame) -> dict:
    """
    Build a name-key → master row index map. Skips rows where any of
    (Surname, First Name, Company Name) is blank. Keeps the first
    occurrence on key collisions (rare — Stage 2 dedupes by surname/
    first/DOB so collisions usually imply same person at multiple
    companies, which the UID join would have already handled).
    """
    if not all(c in master.columns for c in ("Surname", "First Name", "Company Name")):
        return {}
    idx = {}
    for i in range(len(master)):
        surname = str(master.at[i, "Surname"]).strip()
        first   = str(master.at[i, "First Name"]).strip()
        company = str(master.at[i, "Company Name"]).strip()
        if not (surname and first and company):
            continue
        key = _name_key(surname, first, company)
        if key not in idx:
            idx[key] = i
    return idx


def _identity_matches(master, idx, surname, first) -> bool:
    """
    True if the master row at `idx` is the SAME PERSON the Apollo return row
    claims. Guards against a Unique ID that points at a different person — the
    mis-join that happens when the master is renumbered (e.g. a Companies House
    re-fetch) AFTER the return was generated, so an old UID still exists but now
    means someone else.

    Surname must match (case-insensitive, trimmed). First Name is only checked
    when the return supplies one — return files often leave First Name blank, and
    Surname alone is enough to catch a wrong-person UID.
    """
    m_surname = str(master.at[idx, "Surname"]).strip().lower()
    if surname.strip().lower() != m_surname:
        return False
    if first.strip():
        m_first = str(master.at[idx, "First Name"]).strip().lower()
        if first.strip().lower() != m_first:
            return False
    return True


def _uid_surname_conflict(master, apollo) -> tuple:
    """
    Pre-flight check: of an Apollo file's rows whose (non-blank) Unique ID exists
    in the master, how many disagree with the master on Surname? Returns
    (checked, mismatched). A high ratio means the file was produced against a
    DIFFERENTLY NUMBERED master and ingesting it by UID would mis-join wholesale.
    """
    if "Unique ID" not in apollo.columns or "Surname" not in apollo.columns:
        return 0, 0
    uid_to_surname = {str(master.at[i, "Unique ID"]).strip():
                      str(master.at[i, "Surname"]).strip().lower()
                      for i in range(len(master))}
    checked = mism = 0
    for _, row in apollo.iterrows():
        uid = str(row.get("Unique ID", "")).strip()
        sn = str(row.get("Surname", "")).strip().lower()
        if uid and sn and uid in uid_to_surname:
            checked += 1
            if uid_to_surname[uid] != sn:
                mism += 1
    return checked, mism


# If more than this fraction of a file's UID-matchable rows disagree on Surname,
# the file was almost certainly generated against a renumbered master — refuse it.
UID_CONFLICT_REFUSE_THRESHOLD = 0.30


def ingest_batch(project_dir, region_code, apollo_result_path, batch_id, progress_cb=None):
    """
    Join one Apollo result CSV into the master.

    Args:
        project_dir: absolute path to the project folder
        region_code: e.g. "LE1"
        apollo_result_path: path to the uploaded Apollo result CSV (temporary file)
        batch_id: DB id of the apollo_batches row (used for orphan filename)
        progress_cb: optional callable(str)

    Returns:
        dict with keys: matched (int), unmatched (int), total_apollo_rows (int)
    """

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    master_path = os.path.join(project_dir, f"master_{region_code}_raw.csv")
    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")

    # Load current master (use enriched if it exists from a previous batch ingest)
    source_path = enriched_path if os.path.exists(enriched_path) else master_path
    master = pd.read_csv(source_path, dtype=str, keep_default_na=False)
    log(f"Loaded master: {len(master):,} rows from {os.path.basename(source_path)}")

    # Ensure Apollo columns exist (blank if not yet populated)
    for col in APOLLO_INTERNAL_COLS:
        if col not in master.columns:
            master[col] = ""

    # Build lookup indexes. Try UID first (perfect join), fall back to
    # (Surname, First Name, normalised Company Name) when an Apollo row's
    # UID doesn't match anything in our master — that case is common when
    # the operator doesn't have Apollo access themselves and uploads a file
    # from another project's run for the same people.
    uid_to_idx = {uid: idx for idx, uid in enumerate(master["Unique ID"])}
    name_to_idx = _build_name_index(master)

    # Load Apollo result
    try:
        apollo = pd.read_csv(apollo_result_path, dtype=str, keep_default_na=False)
    except Exception as e:
        raise ValueError(f"Could not read Apollo result CSV: {e}")

    log(f"Apollo result: {len(apollo):,} rows, columns: {list(apollo.columns[:6])}...")

    # See ingest_multiple: read enrichment from Apollo's ".1" copy when the
    # uploaded header collides, so we don't fabricate Apollo data from the
    # preserved upload columns. Matching below still uses the preserved columns.
    apollo_src = {c: (f"{c}.1" if f"{c}.1" in apollo.columns else c)
                  for c in APOLLO_RAW_COLS if c in apollo.columns or f"{c}.1" in apollo.columns}

    # Pre-flight: refuse a file that was generated against a renumbered master.
    checked, mism = _uid_surname_conflict(master, apollo)
    if checked and mism / checked > UID_CONFLICT_REFUSE_THRESHOLD:
        raise ValueError(
            f"{mism:,}/{checked:,} rows in this file carry a Unique ID whose "
            f"Surname disagrees with this master. The master was almost certainly "
            f"RENUMBERED after this file was produced (e.g. a Companies House "
            f"re-fetch). Refusing to ingest by Unique ID to avoid mis-joining "
            f"enrichment onto the wrong people. Re-export the return against the "
            f"current master, or re-key it by name."
        )

    uid_matched = 0
    name_matched = 0
    uid_mismatch = 0
    unmatched = 0
    orphan_rows = []

    for _, row in apollo.iterrows():
        idx = None
        uid     = str(row.get("Unique ID", "")).strip()
        surname = str(row.get("Surname", "")).strip()
        first   = str(row.get("First Name", "")).strip()
        company = str(row.get("Company Name", "")).strip()

        # UID join — but only if it points at the SAME person. A UID that exists
        # yet names a different person is a renumber artefact; reject it and let
        # the name fallback handle the row.
        if uid and uid in uid_to_idx:
            cand = uid_to_idx[uid]
            if _identity_matches(master, cand, surname, first):
                idx = cand
                uid_matched += 1
            else:
                uid_mismatch += 1

        # Fall back to name-match. Uses the file's preserved-from-batch
        # Surname / First Name / Company Name columns (NOT Apollo's enriched
        # "Last Name" / "Company Name" outputs, which can differ).
        if idx is None and surname and first and company:
            key = _name_key(surname, first, company)
            if key in name_to_idx:
                idx = name_to_idx[key]
                name_matched += 1

        if idx is None:
            orphan_rows.append(row.to_dict())
            unmatched += 1
            continue

        for raw_col, internal_col in zip(APOLLO_RAW_COLS, APOLLO_INTERNAL_COLS):
            src_col = apollo_src.get(raw_col)
            if src_col:
                val = str(row.get(src_col, "")).strip()
                # Blank-fill only: never overwrite an existing value, so the same
                # person enriched in two files is first-wins, not last-wins.
                if val and not str(master.at[idx, internal_col]).strip():
                    master.at[idx, internal_col] = val

    matched = uid_matched + name_matched
    log(f"  Matched: {matched:,} ({uid_matched:,} by Unique ID, "
        f"{name_matched:,} by name fallback) | "
        f"UID→wrong-person rejected: {uid_mismatch:,} | Unmatched/orphan: {unmatched:,}")
    if uid_mismatch:
        log(f"  WARNING: {uid_mismatch:,} rows carried a Unique ID that exists in "
            f"the master but names a DIFFERENT person — not applied via UID "
            f"(likely a master renumbered after this return was generated).")

    # Guardrail: refuse only if BOTH UID and name-match failed for every row.
    # That's the genuine "wrong file" case — completely different people.
    if len(apollo) > 0 and matched == 0:
        raise ValueError(
            f"None of the {len(apollo):,} rows in this file matched any row "
            f"in the master — neither by Unique ID nor by (Surname, First Name, "
            f"Company Name). The file is almost certainly wrong project data. "
            f"This master's first Unique ID is {master['Unique ID'].iloc[0]}; "
            f"check the file you uploaded belongs to this project or covers "
            f"the same people."
        )

    # Write orphans
    if orphan_rows:
        orphan_path = os.path.join(project_dir, f"apollo_orphans_batch{batch_id}.csv")
        pd.DataFrame(orphan_rows).to_csv(orphan_path, index=False)
        log(f"  {unmatched:,} orphan rows written to {os.path.basename(orphan_path)}")

    # Fan person-level enrichment out across each person's other directorships.
    _fan_out_person_enrichment(master, log)

    # Save enriched master
    master.to_csv(enriched_path, index=False)
    log(f"  Enriched master saved: {os.path.basename(enriched_path)}")

    return {"matched": matched, "unmatched": unmatched, "total_apollo_rows": len(apollo)}


def ingest_multiple(project_dir, region_code, file_paths, progress_cb=None):
    """
    Ingest one or more Apollo result CSVs in a single pass.
    Matches every row across all files by Unique ID — batch order irrelevant.

    Returns:
        dict: matched, unmatched, total_rows, files_processed
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)

    master_path = os.path.join(project_dir, f"master_{region_code}_raw.csv")
    enriched_path = os.path.join(project_dir, f"master_{region_code}_enriched.csv")
    source = enriched_path if os.path.exists(enriched_path) else master_path
    master = pd.read_csv(source, dtype=str, keep_default_na=False)
    log(f"Loaded master: {len(master):,} rows")

    for col in APOLLO_INTERNAL_COLS:
        if col not in master.columns:
            master[col] = ""

    uid_to_idx = {uid: i for i, uid in enumerate(master["Unique ID"])}
    name_to_idx = _build_name_index(master)
    matched = uid_matched = name_matched = uid_mismatch = unmatched = total = 0
    orphans = []
    bad_files = []  # files where >0 rows but 0 matched — wrong-file uploads
    seen_uid_file = {}   # uid -> first filename that claimed it (overlap detection)
    overlap_count = 0

    for path in file_paths:
        try:
            apollo = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception as exc:
            log(f"  Skipping {os.path.basename(path)}: {exc}")
            continue
        total += len(apollo)
        log(f"  {os.path.basename(path)}: {len(apollo):,} rows")

        # Pre-flight: refuse a file produced against a renumbered master. Raised
        # before any master.to_csv(), so a bad upload leaves the master untouched.
        checked, mism = _uid_surname_conflict(master, apollo)
        if checked and mism / checked > UID_CONFLICT_REFUSE_THRESHOLD:
            raise ValueError(
                f"{os.path.basename(path)}: {mism:,}/{checked:,} rows have a Unique "
                f"ID whose Surname disagrees with this master. The master was almost "
                f"certainly RENUMBERED after this file was produced. Refusing to "
                f"ingest by Unique ID to avoid mis-joining enrichment onto the wrong "
                f"people. Re-export the return against the current master, or re-key "
                f"it by name."
            )
        # Apollo's enriched columns come AFTER the preserved upload columns; on a
        # header collision (Apollo also emits "First Name"/"Company Name") pandas
        # suffixes the Apollo copy ".1". Read enrichment from that copy — otherwise
        # we pull the uploaded CH value into the Apollo field and fabricate Apollo
        # data for rows Apollo never matched. (Matching below uses preserved cols.)
        apollo_src = {c: (f"{c}.1" if f"{c}.1" in apollo.columns else c)
                      for c in APOLLO_RAW_COLS if c in apollo.columns or f"{c}.1" in apollo.columns}
        file_matched = 0
        file_uid_matched = 0
        file_name_matched = 0
        fname = os.path.basename(path)
        for _, row in apollo.iterrows():
            idx = None
            uid     = str(row.get("Unique ID", "")).strip()
            surname = str(row.get("Surname", "")).strip()
            first   = str(row.get("First Name", "")).strip()
            company = str(row.get("Company Name", "")).strip()

            # Track cross-file UID overlap (informational — explains first-wins).
            if uid:
                if uid in seen_uid_file and seen_uid_file[uid] != fname:
                    overlap_count += 1
                else:
                    seen_uid_file.setdefault(uid, fname)

            # UID join — only when it names the SAME person (reject renumber drift).
            if uid and uid in uid_to_idx:
                cand = uid_to_idx[uid]
                if _identity_matches(master, cand, surname, first):
                    idx = cand
                    file_uid_matched += 1
                    uid_matched += 1
                else:
                    uid_mismatch += 1

            if idx is None and surname and first and company:
                key = _name_key(surname, first, company)
                if key in name_to_idx:
                    idx = name_to_idx[key]
                    file_name_matched += 1
                    name_matched += 1

            if idx is None:
                orphans.append(row.to_dict())
                unmatched += 1
                continue

            for raw, internal in zip(APOLLO_RAW_COLS, APOLLO_INTERNAL_COLS):
                src = apollo_src.get(raw)
                if src:
                    val = str(row.get(src, "")).strip()
                    # Blank-fill only: never overwrite an existing value (first
                    # file wins) so two uploads of the same UID don't clobber.
                    if val and not str(master.at[idx, internal]).strip():
                        master.at[idx, internal] = val
            matched += 1
            file_matched += 1
        log(f"    → {file_matched:,} matched ({file_uid_matched:,} UID, "
            f"{file_name_matched:,} name fallback)")
        if len(apollo) > 0 and file_matched == 0:
            bad_files.append((fname, len(apollo)))

    # Guardrail: refuse the whole upload if any file had >0 rows but 0 matches.
    # Doing it AFTER the loop so the error message can list every bad file at once.
    # Crucially, no master.to_csv() has run yet — the failed upload leaves the
    # existing enriched master untouched.
    if bad_files:
        first_uid = master["Unique ID"].iloc[0] if len(master) else "(empty master)"
        details = "; ".join(f"{name} ({rows:,} rows, 0 matched)" for name, rows in bad_files)
        raise ValueError(
            f"These files contained no rows matching the master — not by Unique "
            f"ID and not by (Surname, First Name, Company Name) either: {details}. "
            f"This master's first Unique ID is {first_uid}. The files are almost "
            f"certainly for a different set of people. Check they belong to this "
            f"project or cover the same officers."
        )

    if orphans:
        orphan_path = os.path.join(project_dir, "apollo_orphans_multi.csv")
        pd.DataFrame(orphans).to_csv(orphan_path, index=False)
        log(f"  {unmatched:,} orphan rows → apollo_orphans_multi.csv")

    # Fan person-level enrichment out across each person's other directorships.
    _fan_out_person_enrichment(master, log)

    if uid_mismatch:
        log(f"  WARNING: {uid_mismatch:,} rows carried a Unique ID that exists in "
            f"the master but names a DIFFERENT person — not applied via UID "
            f"(likely a master renumbered after these returns were generated).")
    if overlap_count:
        log(f"  Note: {overlap_count:,} Unique IDs appeared in more than one input "
            f"file; blank-fill 'first file wins' was applied to those rows.")

    master.to_csv(enriched_path, index=False)
    log(f"Done: {matched:,} matched ({uid_matched:,} UID, {name_matched:,} name "
        f"fallback), {uid_mismatch:,} UID→wrong-person rejected, {unmatched:,} "
        f"orphans from {len(file_paths)} file(s)")
    return {"matched": matched, "unmatched": unmatched, "total_rows": total,
            "files_processed": len(file_paths), "uid_mismatch": uid_mismatch}
