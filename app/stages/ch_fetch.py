"""
Stage 1 — Companies House fetch.

What it does, in plain English:
  For each postcode (or town name) in the project, ask Companies House for
  every active company at that location, then pull each company's active
  officers, drop the corporate-body officers (we only want real people), and
  deduplicate within the postcode by (Surname, First Name, Company Postcode).
  Saves one results_<AREA>.csv to the project's postcodes/ folder.

  Rate-limited to ~6 req/s, well under CH's 600-per-5-minutes cap.

What's different from the old process:
  Replaces Steps 2–3 of the 18-step manual flow. Used to be: operator opens
  Companies House in a browser, searches each postcode one at a time,
  downloads a CSV per postcode, then cleans/dedupes by hand. The v4 Flask
  app already automated the search; this stage also folds in the dedupe
  and the corporate-body filtering so there's no manual cleaning between
  fetch and Stage 2.
"""
import csv
import os
import time
import requests
from requests.auth import HTTPBasicAuth

# The 27-column schema for the per-postcode CSV.
# Unique ID is blank here — populated at Stage 2 when we assign sequential IDs.
COLUMNS = [
    "Unique ID",
    "Surname",
    "First Name",
    "Middle Names",
    "Officer name",
    "Officer occupation",
    "Officer role",
    "Officer nationality",
    "Officer date of birth",
    "Officer address line one",
    "Officer address locality",
    "Officer address country",
    "Officer address post code",
    "Officer country of residence",
    "Officer appointment date",
    "Appointment",
    "Officer resignation date",
    "Company Name",
    "Company Number",
    "Company Status",
    "Company Type",
    "Company date of creation",
    "Company address line one",
    "Company address locality",
    "Company address country",
    "Company address post code",
    "Company SIC codes",
]

CH_BASE = "https://api.company-information.service.gov.uk"
RATE_SLEEP = 0.17  # ~5.9 req/s


def _auth(api_key):
    return HTTPBasicAuth(api_key, "")


def _parse_name(raw):
    """
    Companies House returns names as 'SURNAME, Firstname Middlenames'.
    Normalise to title case for each part.
    """
    raw = raw.strip()
    if "," in raw:
        parts = raw.split(",", 1)
        surname = parts[0].strip().title()
        rest = parts[1].strip().split()
        first = rest[0].title() if rest else ""
        middle = " ".join(t.title() for t in rest[1:]) if len(rest) > 1 else ""
    else:
        parts = raw.split()
        if len(parts) >= 2:
            surname = parts[-1].title()
            first = parts[0].title()
            middle = " ".join(t.title() for t in parts[1:-1])
        else:
            surname = raw.title()
            first = ""
            middle = ""
    return surname, first, middle


def _is_corporate(name):
    """Heuristic: skip names that look like company names, not people."""
    corporate_markers = {
        "LTD", "LIMITED", "PLC", "LLP", "LLC", "CORP", "CORPORATION",
        "INC", "NOMINEES", "TRUSTEES", "NOMINEE", "CUSTODIAN",
    }
    upper = name.upper()
    # All-caps multi-word names are usually companies
    words = upper.split()
    all_caps = sum(1 for w in words if w.isalpha() and w.isupper() and len(w) > 1)
    if all_caps >= 2:
        return True
    return any(m in upper for m in corporate_markers)


def count_area(area, api_key):
    """
    Make a single lightweight CH API call to get the total active-company count
    for a search area.

    Accepts district-level postcodes ("LE1", "SW1A") or town names ("Leicester").
    Uses the CH `location` parameter which correctly filters by area — unlike
    `registered_office_address.postal_code` which is silently ignored by the API.

    Returns:
        {"area": str, "total": int, "capped": bool, "error": str|None}
        capped=True means total > 10,000 (CH API hard pagination limit)
    """
    auth = _auth(api_key)
    area_clean = area.strip()

    try:
        resp = requests.get(
            f"{CH_BASE}/advanced-search/companies",
            params={
                "location": area_clean,
                "company_status": "active",
                "start_index": 0,
                "size": 1,
            },
            auth=auth,
            timeout=15,
        )
        if resp.status_code == 404:
            return {"area": area, "total": 0, "capped": False,
                    "error": f"Area not recognised by Companies House: '{area_clean}'"}
        if resp.status_code != 200:
            return {"area": area, "total": 0, "capped": False, "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        total = int(data.get("hits", 0))
        return {"area": area, "total": total, "capped": total > 10_000, "error": None}
    except Exception as exc:
        return {"area": area, "total": 0, "capped": False, "error": str(exc)}


def fetch_postcode(area, api_key, output_path, progress_cb=None):
    """
    Fetch all active officers for all companies registered at `area`.

    Accepts district-level postcodes ("LE1", "SW1A") or town names ("Leicester").
    Uses the CH `location` parameter which correctly filters by area.

    Args:
        area: postcode district or town name
        api_key: Companies House API key
        output_path: full path for the output CSV (created/overwritten)
        progress_cb: optional callable(str) for log messages

    Returns:
        int — number of officer rows written
    """
    auth = _auth(api_key)
    area_clean = area.strip()

    def log(msg):
        if progress_cb:
            progress_cb(msg)

    # ── Step 1: collect all companies in this area ────────────────────────────
    companies = []
    seen_numbers = set()
    start = 0
    page = 100

    log(f"Searching companies at {area_clean}...")

    while True:
        try:
            resp = requests.get(
                f"{CH_BASE}/advanced-search/companies",
                params={
                    "location": area_clean,
                    "company_status": "active",
                    "start_index": start,
                    "size": page,
                },
                auth=auth,
                timeout=30,
            )
            # CH returns 500 (not 404) when pagination runs out
            if resp.status_code == 500:
                break
            if resp.status_code == 404:
                log(f"  Area not recognised by Companies House: '{area_clean}'")
                break
            if resp.status_code != 200:
                log(f"  Warning: company search returned {resp.status_code} at offset {start}")
                break

            items = resp.json().get("items", [])
            if not items:
                break

            for c in items:
                num = c.get("company_number", "")
                if num and num not in seen_numbers:
                    seen_numbers.add(num)
                    companies.append(c)

            start += page
            time.sleep(RATE_SLEEP)

        except Exception as exc:
            log(f"  Error fetching companies at offset {start}: {exc}")
            break

    log(f"  {len(companies)} companies found")

    # ── Step 2: fetch officers for each company ───────────────────────────────
    rows = []
    seen_officers = set()  # (surname_lower, first_lower, company_postcode) dedup

    for idx, company in enumerate(companies):
        num = company.get("company_number", "")
        addr = company.get("registered_office_address", {})
        company_postcode = addr.get("postal_code", "")

        try:
            off_resp = requests.get(
                f"{CH_BASE}/company/{num}/officers",
                params={"items_per_page": 100},
                auth=auth,
                timeout=30,
            )
            if off_resp.status_code != 200:
                time.sleep(RATE_SLEEP)
                continue
            officers = off_resp.json().get("items", [])
            time.sleep(RATE_SLEEP)
        except Exception as exc:
            log(f"  Error fetching officers for {num}: {exc}")
            continue

        for officer in officers:
            # Skip resigned officers
            if officer.get("resigned_on"):
                continue

            raw_name = officer.get("name", "")
            if not raw_name:
                continue

            # Skip corporate bodies
            if _is_corporate(raw_name):
                continue

            surname, first, middle = _parse_name(raw_name)

            # Dedup within this postcode by (surname, first, company postcode)
            dedup_key = (surname.lower(), first.lower(), company_postcode.upper())
            if dedup_key in seen_officers:
                continue
            seen_officers.add(dedup_key)

            dob_raw = officer.get("date_of_birth") or {}
            dob = (
                f"{dob_raw.get('month', '')}/{dob_raw.get('year', '')}"
                if dob_raw
                else ""
            )

            off_addr = officer.get("address") or {}

            rows.append({
                "Unique ID": "",
                "Surname": surname,
                "First Name": first,
                "Middle Names": middle,
                "Officer name": raw_name,
                "Officer occupation": officer.get("occupation", ""),
                "Officer role": officer.get("officer_role", ""),
                "Officer nationality": officer.get("nationality", ""),
                "Officer date of birth": dob,
                "Officer address line one": off_addr.get("address_line_1", ""),
                "Officer address locality": off_addr.get("locality", ""),
                "Officer address country": off_addr.get("country", ""),
                "Officer address post code": off_addr.get("postal_code", ""),
                "Officer country of residence": officer.get("country_of_residence", ""),
                "Officer appointment date": officer.get("appointed_on", ""),
                "Appointment": "Active",
                "Officer resignation date": "",
                "Company Name": company.get("company_name") or company.get("title", ""),
                "Company Number": num,
                "Company Status": company.get("company_status", ""),
                "Company Type": company.get("company_type", ""),
                "Company date of creation": company.get("date_of_creation", ""),
                "Company address line one": addr.get("address_line_1", ""),
                "Company address locality": addr.get("locality", ""),
                "Company address country": addr.get("country", ""),
                "Company address post code": company_postcode,
                "Company SIC codes": ", ".join(company.get("sic_codes") or []),
            })

        if (idx + 1) % 50 == 0:
            log(f"  Processed {idx + 1}/{len(companies)} companies...")

    # ── Step 3: write CSV ─────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    log(f"  Done: {len(rows)} rows → {os.path.basename(output_path)}")
    return len(rows)
