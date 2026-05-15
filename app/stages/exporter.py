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
from openpyxl.styles import Font, PatternFill, Alignment

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

FINAL_RE_COLS = ["RE Match?", "Potential", "Match?"]

FINAL_ECHO_COLS = ["Surname", "First Name", "Company Name"]  # echo copies

FINAL_APOLLO_DISPLAY = [
    "First Name", "Last Name", "Title", "Person Linkedin Url",
    "City", "State", "Country", "Email", "Company Name",
    "Website", "Industry", "# Employees", "Annual Revenue", "Total Funding",
    "Company Phone", "Company Linkedin Url", "Company Street", "Company City",
    "Company Postal Code", "Company State", "Company Country", "Company Founded Year",
]
FINAL_APOLLO_INTERNAL = [f"Apollo {c}" for c in FINAL_APOLLO_DISPLAY]


def _pick_master(project_dir, region_code):
    for suffix in MASTER_SUFFIXES:
        path = os.path.join(project_dir, f"master_{region_code}_{suffix}.csv")
        if os.path.exists(path):
            return path, suffix
    raise FileNotFoundError("No master file found. Run at least Stage 2 first.")


def _write_sheet(ws, df, header_fill_hex="1A1A2E"):
    """Write a DataFrame to an openpyxl worksheet with a styled header row."""
    header_fill = PatternFill(start_color=header_fill_hex, end_color=header_fill_hex, fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=9)

    for r_idx, row in enumerate(dataframe_to_rows(df, index=False, header=True), start=1):
        ws.append(row)
        if r_idx == 1:
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(wrap_text=False)

    # Auto-size columns (capped at 50)
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)


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

    # ── Build output DataFrame ────────────────────────────────────────────────
    # We assemble columns in the correct order, inserting blanks for missing ones.
    out = pd.DataFrame()
    out["Unique ID"] = master.get("Unique ID", "")

    for col in FINAL_CH_COLS:
        out[col] = master.get(col, "")

    # Two blank spacer columns (matching v3 layout)
    out["_blank1"] = ""
    out["_blank2"] = ""

    for col in FINAL_RE_COLS:
        out[col] = master.get(col, "")

    # One blank spacer between Potential and Match? (v3 has a gap column)
    out["_blank3"] = ""

    # Re-apply Match? (already set above but v3 has a gap before it)
    # Fix: remove the separate RE col loop to set them manually with spacing
    # (rebuild properly)

    # Rebuild output with correct v3 column ordering
    cols_out = {}
    cols_out["Unique ID"] = master.get("Unique ID", pd.Series([""] * len(master)))

    for col in FINAL_CH_COLS:
        cols_out[col] = master.get(col, pd.Series([""] * len(master)))

    cols_out["_blank1"] = pd.Series([""] * len(master))
    cols_out["_blank2"] = pd.Series([""] * len(master))
    cols_out["RE Match?"] = master.get("RE Match?", pd.Series([""] * len(master)))
    cols_out["Potential"] = master.get("Potential", pd.Series([""] * len(master)))
    cols_out["_blank3"] = pd.Series([""] * len(master))
    cols_out["Match?"] = master.get("Match?", pd.Series([""] * len(master)))

    # Echo copies
    cols_out["_Surname_echo"] = master.get("Surname", pd.Series([""] * len(master)))
    cols_out["_First_echo"] = master.get("First Name", pd.Series([""] * len(master)))
    cols_out["_Company_echo"] = master.get("Company Name", pd.Series([""] * len(master)))

    cols_out["Result"] = master.get("Result", pd.Series([""] * len(master)))

    # Apollo enrichment columns (strip prefix for display)
    for internal, display in zip(FINAL_APOLLO_INTERNAL, FINAL_APOLLO_DISPLAY):
        cols_out[f"_apollo_{display}"] = master.get(internal, pd.Series([""] * len(master)))

    out = pd.DataFrame(cols_out, index=master.index)

    # Sort by Company Name BEFORE the rename step. The v3 layout repeats
    # "Company Name" / "Surname" / "First Name" across the CH, echo, and
    # Apollo column groups, so post-rename the DataFrame has duplicate
    # column labels and sort_values can't pick one. Sorting here uses the
    # CH "Company Name" while it's still unique.
    sort_col = "Company Name"
    if sort_col in out.columns:
        out = out.sort_values(sort_col, key=lambda s: s.str.lower()).reset_index(drop=True)

    # Rename internal names to display names for the final file
    rename_map = {
        "_blank1": "",
        "_blank2": "",
        "_blank3": "",
        "_Surname_echo": "Surname",
        "_First_echo": "First Name",
        "_Company_echo": "Company Name",
    }
    for internal, display in zip(FINAL_APOLLO_INTERNAL, FINAL_APOLLO_DISPLAY):
        rename_map[f"_apollo_{display}"] = display

    out = out.rename(columns=rename_map)

    # ── Build XLSX ────────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()

    # Sheet 1: ALL
    ws_all = wb.active
    ws_all.title = "ALL"
    _write_sheet(ws_all, out)
    log(f"  Sheet 'ALL': {len(out):,} rows")

    # Sheet 2: Y&T
    result_col = out.get("Result", pd.Series([""] * len(out)))
    df_yt = out[out["Result"].isin(["Y", "T"])]
    ws_yt = wb.create_sheet("Y&T")
    _write_sheet(ws_yt, df_yt)
    log(f"  Sheet 'Y&T': {len(df_yt):,} rows")

    # Sheet 3: Potential RE Match
    df_re = out[out["RE Match?"] == "Y"]
    ws_re = wb.create_sheet("Potential RE Match")
    _write_sheet(ws_re, df_re)
    log(f"  Sheet 'Potential RE Match': {len(df_re):,} rows")

    out_path = os.path.join(project_dir, f"{region_code}_final_deliverable.xlsx")
    wb.save(out_path)
    log(f"  Saved: {os.path.basename(out_path)}")

    return out_path
