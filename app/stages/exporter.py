"""
Stage 8 — Final segmentation + XLSX export (the Treasurers' handover file).

What it does, in plain English:
  Read the most complete master that exists in the project folder
  (prefers re_flagged > vs > classified > enriched > raw, so partial
  pipelines still produce something usable), then write a multi-tab
  XLSX that matches the existing Leicester v3 deliverable shape:
    - ALL                 every row, all columns
    - Y&T                 rows where Result is Y or T
    - Potential RE Match  rows where RE Match? is Y
  Every tab carries the same golden-v3 column layout (Charles,
  2026-06-12: the columns must match the golden standard; the per-tab
  value is the row segmentation, not a different column set).
  Apollo columns lose the internal "Apollo " prefix on the way out so
  the file looks identical to the Treasurers' current format.

What's different from the old process:
  Replaces Steps 16–17. Used to be: manual segmentation, copy-paste
  into a fresh workbook, hand-build the tabs. The whole point of this
  stage is to be INVISIBLE to the Treasurers — same column names,
  same tab names, same look. The work happens upstream; only the
  way the file is produced changes.
"""
import os
import pandas as pd
import openpyxl
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

# Master file precedence (most enriched to least)
MASTER_SUFFIXES = ["re_flagged", "vs", "classified", "enriched", "raw"]

# Columns to include in the final output, in order.
# The "Apollo " prefix is stripped for display (back to raw Apollo names).
# Columns that don't exist in the master are written as blank.
FINAL_CH_COLS = [
    "Surname", "First Name", "Middle Names", "Officer name",
    "Officer occupation", "Officer nationality",
    "Officer date of birth",
    "Officer address line one", "Officer address locality",
    "Officer address country", "Officer address post code",
    "Officer country of residence", "Officer appointment date",
    "Company Name", "Company Number", "Company Status", "Company Type",
    "Company date of creation",
    "Company address line one", "Company address locality",
    "Company address country", "Company address post code",
    "Company SIC codes",
]

FINAL_ECHO_COLS = ["Surname", "First Name", "Company Name"]  # echo copies

FINAL_APOLLO_DISPLAY = [
    "First Name", "Last Name", "Title", "Person Linkedin Url",
    "City", "State", "Country", "Email", "Company Name",
    "Website", "Industry", "# Employees", "Annual Revenue", "Total Funding",
    "Company Phone", "Company Linkedin Url", "Company Street", "Company City",
    "Company Postal Code", "Company State", "Company Country", "Company Founded Year",
]
FINAL_APOLLO_INTERNAL = [f"Apollo {c}" for c in FINAL_APOLLO_DISPLAY]

# VoteSource return columns (the O–AE block of the canonical `vs example.xlsx`),
# in order. Folded into the master at Stage 6b and appended to the deliverable
# only when present — so the default file stays identical to the golden v3
# (no-VS) shape, and gains these exact headers once a VS overlay has returned.
VS_RETURN_COLS = [
    "ConstituentId", "ConstituentDateOfBirth", "VSAge", "ConstituentFullName",
    "CanBeContacted", "CanBeEmailed", "AddressFullName",
    "LastKnownVotingIntention", "LastKnownVotingIntentionDate", "EmailAddress",
    "ConstituentEmailDataStatementConsentObtainedEmail", "ConstituentEmailConsentDate",
    "TelephonePhoneNumber", "ConstituentTelephoneConsentDate",
    "Mosaic Code 2025", "Household Income Band (2025)", "Personal Income Band (2025)",
]


def _pick_master(project_dir, region_code):
    for suffix in MASTER_SUFFIXES:
        path = os.path.join(project_dir, f"master_{region_code}_{suffix}.csv")
        if os.path.exists(path):
            return path, suffix
    raise FileNotFoundError("No master file found. Run at least Stage 2 first.")


def _write_sheet(ws, df, header_fills=None, spacer_cols=None):
    """
    Write a DataFrame to a worksheet with the golden v3 ease-of-use features
    (Charles, 2026-06-12: bold Calibri header, frozen header row, auto-filter
    across all columns) plus section colouring and text-fitted column widths
    (Harry, 2026-06-12): header cells coloured by the stage that produced
    their block, columns sized to their content, and the blank spacer columns
    narrowed + filled so they read as dividers between sections.

    header_fills: hex string per column (None = plain bold header cell).
    spacer_cols: 0-based indices of the blank spacer columns.
    """
    for row in dataframe_to_rows(df, index=False, header=True):
        ws.append(row)

    fill_cache = {}

    def _fill(hex_code):
        if hex_code not in fill_cache:
            fill_cache[hex_code] = PatternFill(start_color=hex_code,
                                               end_color=hex_code, fill_type="solid")
        return fill_cache[hex_code]

    for i, cell in enumerate(ws[1]):
        if header_fills and i < len(header_fills) and header_fills[i]:
            cell.fill = _fill(header_fills[i])
            cell.font = Font(bold=True, color="FFFFFF")
        else:
            cell.font = Font(bold=True)

    # Fit each column to its longest value (capped so one long URL doesn't
    # blow a column out to a full screen width).
    for col in ws.columns:
        max_len = max((len(str(cell.value)) for cell in col if cell.value is not None),
                      default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    # Spacer columns become narrow grey dividers, filled top to bottom
    # (after auto-fit, so they stay narrow).
    for ci in (spacer_cols or []):
        ws.column_dimensions[get_column_letter(ci + 1)].width = 2.5
        for r in range(1, ws.max_row + 1):
            ws.cell(row=r, column=ci + 1).fill = _fill("D9D9D9")

    ws.freeze_panes = "A2"
    if len(df):
        ws.auto_filter.ref = ws.dimensions


def build_export(project_dir, region_code, progress_cb=None):
    """
    Build the final deliverable XLSX.

    Returns:
        str — path to the generated XLSX file
    """

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    master_path, used_suffix = _pick_master(project_dir, region_code)
    log(f"Using master: {os.path.basename(master_path)} (suffix: {used_suffix})")

    master = pd.read_csv(master_path, dtype=str, keep_default_na=False)
    log(f"  {len(master):,} rows loaded")

    # ── Build output DataFrame (golden v3 column order + values) ──────────────
    # The deliverable mirrors `Leicester - Data v3.xlsx` exactly so the Treasurers
    # don't have to learn a new format. Two golden mappings happen *here at export
    # only* — the pipeline keeps Y/T/N internally:
    #   - "Match?" column = the sanity check, shown as Yes / Tentative / No
    #     (mapped from the master's internal Result = Y/T/N).
    #   - "Result" column = "Matched" if Apollo returned a contact, else "N/A".
    # The RE-flagger output lives in "RE Match?" / "Potential" (golden's home for
    # it); the internal RE confidence (confirmed/human) is not shown in the file.
    n = len(master)

    def mcol(name):
        return master.get(name, pd.Series([""] * n, index=master.index))

    SANITY_TO_WORD = {"Y": "Yes", "T": "Tentative", "N": "No"}
    sanity = mcol("Result").map(lambda v: SANITY_TO_WORD.get(str(v).strip(), ""))
    apollo_present = (mcol("Apollo First Name").astype(str).str.strip() != "") | \
                     (mcol("Apollo Email").astype(str).str.strip() != "")
    blank = pd.Series([""] * n, index=master.index)

    cols_out = {"Unique ID": mcol("Unique ID")}
    for col in FINAL_CH_COLS:
        cols_out[col] = mcol(col)
    cols_out["_blank1"] = blank
    cols_out["_blank2"] = blank
    # Golden RE-column semantics (verified against Leicester v3, 2026-06-12):
    #   "RE Match?" held the word 'Potential' — the certainty slot. Charles's
    #   tiers (2026-06-10) are the new layer here: Match / Probable / Potential.
    #   "Potential" held the matched RE donor's NAME — that display is the
    #   "matching logic we currently have" Charles said not to lose.
    cols_out["RE Match?"] = mcol("Potential")   # tier (master keeps it there)
    cols_out["Potential"] = mcol("RE Name")     # who matched, as golden showed
    cols_out["_blank3"] = blank
    cols_out["Match?"] = sanity                       # golden: sanity check (Yes/No/Tentative)
    cols_out["_Surname_echo"] = mcol("Surname")
    cols_out["_First_echo"] = mcol("First Name")
    cols_out["_Company_echo"] = mcol("Company Name")
    cols_out["Result"] = apollo_present.map(lambda x: "Matched" if x else "N/A")
    for internal, display in zip(FINAL_APOLLO_INTERNAL, FINAL_APOLLO_DISPLAY):
        cols_out[f"_apollo_{display}"] = mcol(internal)

    # VoteSource overlay (Stage 6b): append the canonical vs-example return
    # columns ONLY when present, so the no-VS deliverable still matches golden v3.
    vs_present = any(c in master.columns for c in VS_RETURN_COLS)
    if vs_present:
        cols_out["_blank_vs"] = blank
        for c in VS_RETURN_COLS:
            cols_out[c] = mcol(c)
        log(f"  VoteSource overlay present — appended {len(VS_RETURN_COLS)} VS column(s)")

    # Internal filter key for the Potential RE Match tab — dropped before write.
    cols_out["_re_flag"] = mcol("RE Match?").map(lambda v: "Y" if str(v).strip() == "Y" else "")

    out = pd.DataFrame(cols_out, index=master.index)

    # Sort by Unique ID ascending — this is the golden v3 row order (its rows
    # run #LE1-0005, 0008, 0009, 0017 … i.e. UID-ascending, NOT company-name
    # order). Sort on the numeric suffix, not the string: UIDs aren't padded to
    # a fixed width (#ESSEX-9999 then #ESSEX-10000), so a lexicographic sort
    # would interleave them wrongly. Rows with no UID (shouldn't happen post
    # Stage 2) sort to the end. Done before the rename step for consistency with
    # the rest of the column handling.
    if "Unique ID" in out.columns:
        uid_num = pd.to_numeric(
            out["Unique ID"].str.extract(r"(\d+)\s*$", expand=False), errors="coerce"
        )
        out = (out.assign(_uid_num=uid_num)
                  .sort_values("_uid_num", kind="stable", na_position="last")
                  .drop(columns="_uid_num")
                  .reset_index(drop=True))

    # Rename internal names to display names for the final file
    rename_map = {
        "_blank1": "",
        "_blank2": "",
        "_blank3": "",
        "_blank_vs": "",
        "_Surname_echo": "Surname",
        "_First_echo": "First Name",
        "_Company_echo": "Company Name",
    }
    for internal, display in zip(FINAL_APOLLO_INTERNAL, FINAL_APOLLO_DISPLAY):
        rename_map[f"_apollo_{display}"] = display

    out = out.rename(columns=rename_map)

    # Split off the internal RE-filter key before writing.
    re_mask = out["_re_flag"] == "Y"
    out = out.drop(columns=["_re_flag"])

    # ── Section colouring: one header fill per column block (Harry 2026-06-12)
    # Mirrors the column construction above — keep in step with cols_out.
    NAVY, BURGUNDY, GREEN, GREY, BLUE = "1A1A2E", "6E2234", "2F6D4F", "595959", "2C5F8A"
    header_fills = (
        [NAVY] * (1 + len(FINAL_CH_COLS))      # Unique ID + CH block (stages 1-2)
        + [None, None]                          # spacers
        + [BURGUNDY, BURGUNDY]                  # RE Match? (tier), Potential (donor) — stage 7
        + [None]                                # spacer
        + [GREEN]                               # Match? — sanity check (stage 5)
        + [GREY] * (len(FINAL_ECHO_COLS) + 1)   # echoes + Result (Apollo bookkeeping)
        + [GREY] * len(FINAL_APOLLO_DISPLAY)    # Apollo data (stage 4)
    )
    if vs_present:
        header_fills += [None] + [BLUE] * len(VS_RETURN_COLS)  # VoteSource (stage 6)
    assert len(header_fills) == len(out.columns), \
        f"header_fills ({len(header_fills)}) out of step with columns ({len(out.columns)})"
    spacer_cols = [i for i, c in enumerate(out.columns) if c == ""]

    # ── Build XLSX ────────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()

    # Sheet 1: ALL
    ws_all = wb.active
    ws_all.title = "ALL"
    _write_sheet(ws_all, out, header_fills, spacer_cols)
    log(f"  Sheet 'ALL': {len(out):,} rows")

    # Sheet 2: Y&T — sanity check Yes/Tentative AND not RE-flagged. The RE
    # match takes precedence (Charles, 2026-06-12): existing donors must not
    # land on the cold-outreach list. Verified against golden — Leicester's 56
    # flagged UIDs appear on ALL and the RE tab but NEVER on Y&T, even the 48
    # whose sanity check passed.
    yt_mask = out["Match?"].isin(["Yes", "Tentative"])
    df_yt = out[yt_mask & ~re_mask]
    ws_yt = wb.create_sheet("Y&T")
    _write_sheet(ws_yt, df_yt, header_fills, spacer_cols)
    diverted = int((yt_mask & re_mask).sum())
    log(f"  Sheet 'Y&T': {len(df_yt):,} rows"
        + (f" ({diverted} RE-flagged row(s) live on the RE tab instead)" if diverted else ""))

    # Sheet 3: Potential RE Match
    df_re = out[re_mask]
    ws_re = wb.create_sheet("Potential RE Match")
    _write_sheet(ws_re, df_re, header_fills, spacer_cols)
    log(f"  Sheet 'Potential RE Match': {len(df_re):,} rows")

    out_path = os.path.join(project_dir, f"{region_code}_final_deliverable.xlsx")
    wb.save(out_path)
    log(f"  Saved: {os.path.basename(out_path)}")

    return out_path
