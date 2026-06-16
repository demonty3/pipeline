"""
Stage 3 — Apollo batch magazine.

What it does, in plain English:
  Apollo's bulk-CSV uploader caps each file at 10,000 rows. This stage
  prepares the queue of upload files — the "magazine" of ready-to-fire
  batches — so the operator just picks them up one at a time:
    - Drop duplicate persons (already flagged in Stage 2) so we don't
      pay Apollo twice for the same person
    - Apply the operator pre-filter (skip dissolved companies) so
      credits aren't spent on noise
    - Apply the cleaned form of Company Name (see text_cleanup) to lift
      Apollo's match rate vs. the raw uppercase CH names
    - Split the remainder into ≤10,000-row batch CSVs, each tagged with
      Unique IDs so Stage 4 can rejoin perfectly

  Each batch CSV: Unique ID | Surname | First Name | Company Name.

What's different from the old process:
  Replaces the prep half of Step 6. Used to be: the operator manually
  decided where to split the master, made files by hand, with no
  dedupe — meaning the same person could appear in multiple batches and
  burn credits twice. The within-project dedupe and the cleaned Company
  Name are net-new compared to the manual flow.
"""
import os
import math
import pandas as pd

from stages.text_cleanup import clean_company_name

BATCH_SIZE = 10_000  # Apollo's hard per-document cap


def build_batches(project_dir, region_code, exclude_dissolved=True,
                  credit_budget=None, batch_size=None, progress_cb=None):
    """
    Build apollo_batch_NNN.csv files from the Stage 2 master.

    Args:
        project_dir: absolute path to the project folder
        region_code: e.g. "LE1"
        exclude_dissolved: drop rows where Company Status == 'Dissolved'
        credit_budget: optional int — cap total upload rows at this number to
            stay within Apollo's remaining credit pool. Rows beyond the cap
            are written to apollo_deferred_<region>.csv so the operator can
            queue them for the next run.
        batch_size: optional int — rows per batch file. Defaults to Apollo's
            10,000 cap and is clamped to that ceiling; smaller values let the
            operator generate more, smaller files (e.g. for parallel uploads).
        progress_cb: optional callable(str)

    Returns:
        list of dicts: [{batch_num, row_count, filename}, ...]
    """

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    master_path = os.path.join(project_dir, f"master_{region_code}_raw.csv")
    if not os.path.exists(master_path):
        raise FileNotFoundError(f"master_{region_code}_raw.csv not found — run Stage 2 first.")

    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    log(f"Loaded master: {len(master):,} rows")

    # ── Region-wide person dedup: one row per unique person ───────────────────
    # Stage 2 flags every repeat of a person (same Surname|First|DOB) across ALL
    # postcodes as an Apollo Duplicate. Uploading only the non-duplicates means
    # each person is enriched once no matter how many companies they direct —
    # the fewest possible upload rows (and files). Stage 4 then fans the person's
    # enrichment back across their other rows on the way in.
    if "Apollo Duplicate" in master.columns:
        dup_mask = master["Apollo Duplicate"].str.lower() == "true"
        upload_list = master[~dup_mask].copy()
        log(f"  Region-wide person dedup: {int(dup_mask.sum()):,} duplicate-person rows held back, "
            f"{len(upload_list):,} unique people to upload")
    else:
        upload_list = master.copy()
        log(f"  {len(upload_list):,} rows (no dedup flag present)")

    # ── Optional pre-filters ─────────────────────────────────────────────────
    if exclude_dissolved and "Company Status" in upload_list.columns:
        before = len(upload_list)
        upload_list = upload_list[upload_list["Company Status"].str.strip().str.lower() != "dissolved"]
        log(f"  Excluded dissolved: {before - len(upload_list):,} rows dropped ({len(upload_list):,} remain)")

    # ── Build Apollo upload columns ────────────────────────────────────────────
    # Column shape matches the canonical SW3 example: Surname, First Name,
    # Company Name. Unique ID prepended so Apollo's result round-trip rejoins
    # cleanly by ID in Stage 4.
    apollo_df = pd.DataFrame({
        "Unique ID": upload_list["Unique ID"],
        "Surname": upload_list["Surname"],
        "First Name": upload_list["First Name"],
        "Company Name": upload_list["Company Name"].apply(clean_company_name),
    })

    # ── Credit-budget cap ──────────────────────────────────────────────────────
    # If the operator (or Apollo's API) says only N credits are available,
    # cap this run at N rows and defer the rest to apollo_deferred_<region>.csv.
    if credit_budget is not None and credit_budget > 0 and len(apollo_df) > credit_budget:
        deferred = apollo_df.iloc[credit_budget:].copy()
        apollo_df = apollo_df.iloc[:credit_budget].copy()
        deferred_path = os.path.join(project_dir, f"apollo_deferred_{region_code}.csv")
        deferred.to_csv(deferred_path, index=False)
        log(f"  Credit budget {credit_budget:,}: keeping {len(apollo_df):,} rows, "
            f"deferring {len(deferred):,} → {os.path.basename(deferred_path)}")
    elif credit_budget is not None and credit_budget > 0:
        log(f"  Credit budget {credit_budget:,}: covers all {len(apollo_df):,} rows, no defer needed")

    # ── Split into batches ────────────────────────────────────────────────────
    if batch_size is None or batch_size <= 0:
        size = BATCH_SIZE
    else:
        size = min(batch_size, BATCH_SIZE)
    total = len(apollo_df)
    n_batches = max(1, math.ceil(total / size))
    log(f"  Splitting {total:,} rows into {n_batches} batch(es) of ≤{size:,}")

    batches = []
    for i in range(n_batches):
        chunk = apollo_df.iloc[i * size : (i + 1) * size]
        batch_num = i + 1
        filename = f"apollo_batch_{batch_num:03d}.csv"
        out_path = os.path.join(project_dir, filename)
        chunk.to_csv(out_path, index=False)
        log(f"  Batch {batch_num:03d}: {len(chunk):,} rows → {filename}")
        batches.append({"batch_num": batch_num, "row_count": len(chunk), "filename": filename})

    return batches
