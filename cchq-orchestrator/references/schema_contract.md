# Schema contract — the load-bearing format

Every stage adds columns to a single master table **without re-ordering or
filtering rows**. The `Unique ID` (`#LE1-0001` style) is the join key the whole
pipeline hangs on. If a column name, column order, or the spacer/echo layout
drifts, downstream joins silently mis-merge and the Treasurers' deliverable
stops matching the format they know. This file is the source of truth for that
format. It was reconciled against the real golden files in Drive
(`Leicester - Data v1.xlsx` Sheet1 and `Leicester - Data v3.xlsx` ALL tab) and
the stage code (`app/stages/merge.py`, `apollo_ingest.py`, `exporter.py`).

**Rule of thumb before running any stage:** the input file must already carry
the columns the previous stage was supposed to add. Use `run_stage.py status`
to confirm which `master_<REGION>_*.csv` exists before advancing.

---

## Master file lineage (one file per stage, never overwritten in place)

| After stage | File written                       | Columns it adds |
|-------------|------------------------------------|-----------------|
| 2 merge     | `master_<RC>_raw.csv`              | `Unique ID` + 26 CH cols + `SIC Industry`, `Directorships`, `Apollo Duplicate` |
| 4 ingest    | `master_<RC>_enriched.csv`         | + 22 `Apollo <col>` columns |
| 5 classify  | `master_<RC>_classified.csv`       | + `Result` (Y / T / N) |
| 6 vs-return | `master_<RC>_vs.csv`               | + whatever columns the VoteSource return carried |
| 7 re-flag   | `master_<RC>_re_flagged.csv`       | + `RE Match?`, `Potential`, `RE Name`, `Match?` |
| 8 export    | `<RC>_final_deliverable.xlsx`      | multi-tab, reshaped to v3 layout (see below) |

Stage 8 reads the **most enriched** master that exists
(`re_flagged > vs > classified > enriched > raw`), so a partial pipeline still
produces a usable file.

---

## The 26 Companies House columns (exact order — from `merge.py`)

```
Surname, First Name, Middle Names, Officer name, Officer occupation,
Officer role, Officer nationality, Officer date of birth,
Officer address line one, Officer address locality, Officer address country,
Officer address post code, Officer country of residence,
Officer appointment date, Appointment, Officer resignation date,
Company Name, Company Number, Company Status, Company Type,
Company date of creation, Company address line one, Company address locality,
Company address country, Company address post code, Company SIC codes
```

`Unique ID` sits **first**, before this block. After the block, merge appends
its three internal columns: `SIC Industry`, `Directorships`, `Apollo Duplicate`.

- **Directorships** — count of (Surname, First Name) across the region.
- **Apollo Duplicate** — `True` for repeat persons by (Surname, First Name,
  Officer date of birth). Stage 3 uploads only the `False` rows (dedup saves
  the redundant Apollo lookups).

## The 22 Apollo columns (exact Apollo export names — from `apollo_ingest.py`)

```
First Name, Last Name, Title, Person Linkedin Url, City, State, Country,
Email, Company Name, Website, Industry, # Employees, Annual Revenue,
Total Funding, Company Phone, Company Linkedin Url, Company Street,
Company City, Company Postal Code, Company State, Company Country,
Company Founded Year
```

In the master these are stored **prefixed** as `Apollo First Name`,
`Apollo Email`, etc. — the prefix prevents collisions with the CH columns of
the same name (`Company Name`, `First Name`...). Stage 8 strips the prefix on
the way out so the deliverable shows the raw Apollo names.

**Ingest join order:** Unique ID first, then a `(Surname, First Name,
normalised Company Name)` fallback for files whose IDs don't line up (e.g. an
Apollo export run from a different operator's account). Company names are
normalised through `clean_company_name()` on both sides so a raw CH name
matches Apollo's already-cleaned name.

## Classifier / RE-flagger columns

These are the **internal master** columns; the Stage 8 deliverable remaps them to
the golden v3 format (see the mapping note below).

- Stage 5 writes `Result` ∈ {`Y`, `T`, `N`} — the sanity check (does the Apollo
  contact match the CH officer). Cascade: deterministic fuzzy score (≥85 → Y,
  <45 → N, middle → Tentative) → deterministic evidence pass on the Tentative
  band (core-name agreement ignoring middle names, nickname re-score with a
  surname-contradiction guard, email corroboration; upgrade-only, T → Y) →
  only no-evidence rows reach human review. (Gemini removed 2026-06-11.)
- Stage 7 writes `RE Match?` (`Y`/blank), `Potential`, and an internal `Match?`
  (the audit factor string, e.g. `name 95, email exact`). **Formulas only — RE
  donor data is highly sensitive and must never reach an LLM (Charles,
  2026-06-10).** `Potential` carries the certainty tier:
  `Match` (exact email + plausible name) > `Probable` (exact email with weak
  name, or strong name + same postcode/town) > `Potential` (name similarity
  alone — never elevated without a corroborating factor). All three tiers get
  `RE Match? = Y` and land on the Potential RE Match tab.
  (The 2026-06-12 full-postcode/surname tightening was reverted the same day —
  Harry: keep the matching aligned with how the golden deliverable was
  produced.)

---

## Stage 8 deliverable layout (the v3 shape — DO NOT drift)

Matches `Leicester - Data v3.xlsx` exactly. Three tabs:

- **ALL** — every row, full column set below.
- **Y&T** — rows where the sanity check is Yes/Tentative (deliverable `Match?` ∈
  {`Yes`, `Tentative`}; internally `Result` ∈ {`Y`, `T`}).
- **Potential RE Match** — rows where `RE Match?` == `Y`.

Column order on every tab (note the deliberate **blank spacer columns** and the
**echo copies** of Surname / First Name / Company Name that sit between the CH
block and the Apollo block — the Treasurers' sheet has always looked like this):

```
Unique ID,
<23 CH display cols: Surname … Company SIC codes>,   (note: drops Officer role,
                                                       Appointment, Officer
                                                       resignation date vs the
                                                       26-col master)
<blank>, <blank>,
RE Match?, Potential, <blank>, Match?,
                                   (golden semantics, confirmed 2026-06-12:
                                    RE Match? = the certainty slot — golden
                                    showed 'Potential'; now the tier word
                                    Match/Probable/Potential. Potential = the
                                    matched RE donor's NAME, as golden.)
Surname (echo), First Name (echo), Company Name (echo), Result,
<22 Apollo cols, prefix stripped: First Name … Company Founded Year>
```

**Golden value mapping (applied at export only — the master keeps `Y`/`T`/`N`).**
Confirmed against the real `Leicester - Data v3.xlsx` cells (7,244 rows): the
deliverable's **`Match?`** column holds the *sanity check* as `Yes` / `No` /
`Tentative` (mapped from the master's `Result`), and the **`Result`** column holds
`Matched` / `N/A` (whether Apollo returned a contact). The RE-flagger's internal
factor string (master `Match?`) is NOT shown — RE lives in `RE Match?` / `Potential`.

Sort: by `Unique ID` ascending, matching golden v3 (its rows run
`#LE1-0005, 0008, 0009, 0017 …` — UID order, *not* company-name order). Sort on
the **numeric suffix**, not the raw string — UIDs aren't zero-padded to a fixed
width (`#ESSEX-9999` then `#ESSEX-10000`), so a lexicographic sort interleaves
them wrongly.

**If you change anything in this file, regenerate one project end-to-end and
diff the Stage 8 output against `Leicester - Data v3.xlsx` before shipping.**
