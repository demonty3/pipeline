# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# CCHQ Business Club Building

Internal data-pipeline rebuild for CloudMundi. The team currently runs an 18-step manual process — postcode-driven Companies House search, Apollo enrichment, sanity-checking, Raiser's Edge cross-reference, VoteSource overlay — to produce ranked donor-outreach lists for the Conservative Party's CCHQ Business Club events. This project replaces it with a small project-orchestrator web app.

## Read first

The authoritative brief is **`CCHQ Project Scope - Concept v1.docx`**. Read it before making any structural decision. Section 5 defines the 8 orchestrator stages; Section 6 covers the four first-class features (Apollo batch magazine, Y/T/N auto-classifier, RE fuzzy flagger, async VoteSource pause/resume); Section 8 lists what's explicitly out of scope.

Supporting reference files in this folder:

- `CCHQ - Project Flow.docx` — the original 18-step manual process, for context on what's being replaced.
- `App_Documentation_v4.docx` — spec for the existing Flask app. It implements Stages 1–2 (Companies House postcode search + CSV name enrichment) and is a useful reference for those stages. The rebuild is *not* a fork of it — reuse patterns where they help, ignore where they don't.
- `Leicester - Data v1.xlsx` — real master-sheet example. Sheet1 is the canonical schema (26 Companies House columns + Unique ID + sanity-check column + 22 Apollo columns).
- `Leicester - Data v2.xlsx` — intermediate state with the Raiser's Edge name list on its own tab.
- `Leicester - Data v3.xlsx` — the final handover file, multi-tab (`ALL`, `Y&T`, `Potential RE Match`).
- `re export headers.ods` — sample of the raw Raiser's Edge export structure (36 columns; only the Name column is currently used).
- `results_LE2.csv` — raw Companies House output for one postcode, produced by the v4 app.

## Commands

The app and the headless driver both **must be launched from `app/`** — `DB_PATH`, the
projects base dir, and `load_dotenv()` all resolve relative to that directory.

- **Setup (once):** `./setup.sh` (macOS) or `setup.bat` (Windows) — creates `.venv` at
  the repo root, installs `app/requirements.txt`, seeds `app/.env` from `.env.example`.
- **Run the web app:** `./run.sh` (or `run.bat`) → http://localhost:5050. Equivalent:
  `cd app && ../.venv/bin/python app.py`.
- **Headless stage driver (what the orchestrator/loop uses):**
  `cd app && python ../cchq-orchestrator/scripts/run_stage.py --region <RC> <stage>`.
  Stages: `init fetch merge magazine ingest classify vs-export vs-return re-flag export
  summary status`. Useful options: `--area`, `--id-prefix`, `--budget N` (magazine),
  `--files f…` (ingest), `--file f` (vs-return / re-flag), `--force` (re-run merge —
  blocked by default because it re-mints every Unique ID).
- **Run one test:** `cd app && python3 tests/test_evidence_pass.py`. Tests are
  standalone scripts with their own `__main__` runners (no pytest; it isn't installed).
  Files: `app/tests/test_*.py` and `app/test_misjoin_fixes.py`.
- **Env:** only `CH_API_KEY` is required (Stage 1 fetch). `APOLLO_API_KEY` is optional
  (live credit lookup only). Both live in `app/.env`.

## Architecture

**Two surfaces over one store.** A Flask web app (`app/app.py`, port 5050, `debug=True`,
`use_reloader=False`) and a headless CLI (`cchq-orchestrator/scripts/run_stage.py`) both
read/write the same SQLite index (`app/projects.db`, see `app/database.py`) and the same
per-project folders (`app/projects/<id>_<REGION>/`). The web app is the review/eyeball
surface; the CLI is what the `cchq-orchestrator` skill and its `/loop` automation drive.
Anything one surface does is visible to the other.

**The pipeline is a chain of master files joined on Unique ID.** Each stage in
`app/stages/` reads the previous stage's master and writes the next; nothing is
overwritten, and Stage 8 always exports from the most complete master present. The
`#LE1-0001`-style Unique ID minted at Stage 2 is the load-bearing join key for all
downstream work (Apollo enrichment, classifier and RE decisions) — never re-mint it on
existing rows.

| Stage | File | Writes |
|---|---|---|
| 1 fetch | `ch_fetch.py` | `postcodes/results_<AREA>.csv` (one per postcode) |
| 2 merge | `merge.py` | `master_<RC>_raw.csv` (+ Unique IDs, SIC labels) |
| 3 magazine | `apollo_magazine.py` | `apollo_batch_NNN.csv` (≤10k each; uploaded to Apollo by hand) |
| 4 ingest | `apollo_ingest.py` | `master_<RC>_enriched.csv` |
| 5 classify | `classifier.py` | `master_<RC>_classified.csv` + `classifications_log.csv` |
| 6 vs-export/return | `vs_export.py` | `vs_export_<RC>.xlsx` / `master_<RC>_vs.csv` |
| 7 re-flag | `re_flagger.py` | `master_<RC>_re_flagged.csv` |
| 8 export | `exporter.py` | `<RC>_final_deliverable.xlsx` (tabs `ALL` / `Y&T` / `Potential RE Match`) |

`project_merge.py` stacks ≥2 projects into a new one without mutating the sources,
preserving UIDs and refusing on collision. `text_cleanup.py`, `credit_chop.py`, and
`apollo_credits.py` are shared helpers.

**State model.** `projects` carries `stage1_status`…`stage8_status` per project;
`stage_logs`, `apollo_batches`, and `stage5_decisions` / `stage7_decisions` (the audit
trails) are separate tables. In the web app, long stages (1, 5) run as daemon threads
and the rest run synchronously in-request; a server restart kills any running thread, so
`fail_stale_running_stages()` surfaces those as errors on boot (this is why the reloader
is disabled).

**The `cchq-orchestrator/` skill** holds the automation layer: `SKILL.md`,
`scripts/run_stage.py` (the engine above), `references/` (`schema_contract.md` =
normative format + RE tier rules; `loop_runbook.md`; `gmail_playbook.md`), and a
`state/` dir of per-run files (gitignored).

## Standing constraints

- **Vibe-coder context.** Harry is an intern who codes with AI assistance, not a deep software engineer. Prefer simple, well-supported, maintainable choices. Conventional over clever.
- **Storage is filesystem-first.** Master tables live as CSV/Parquet/XLSX inside per-project folders. A small SQLite index for project metadata is fine; a full database is overkill at this scale.
- **Python is the default.** The existing v4 app is Python/Flask; reusing that ecosystem is the path of least resistance unless there's a strong reason to switch.
- **Schema continuity is load-bearing.** The master table keeps the existing 26 Companies House columns + Unique ID + 22 Apollo columns + `Match?` + RE-flag fields. The Treasurers' final deliverable must stay shape-compatible with the current handover — they shouldn't have to learn a new file format.
- **Apollo bulk upload caps at 10,000 rows per document.** Any batching logic has to respect this. The "batch magazine" pattern in the scope is the canonical design for this.
- **No LLM anywhere in the pipeline** (decision: Harry, 2026-06-11). Stage 5's second pass used to call Gemini Flash; free-tier quota stalls made it unreliable, so it was replaced by a deterministic *evidence pass* (core-name agreement ignoring middle names + nickname canonicalisation + email-corroborates-officer check, with a surname-contradiction guard on the fuzzy upgrade) in `app/stages/classifier.py`. Cascade is: deterministic fuzzy score → evidence pass on the tentative band (upgrade-only, T→Y) → human review on the no-evidence residue. Every decision still logs label + confidence + reason for audit. `GEMINI_API_KEY` is no longer used — don't reintroduce an LLM into any stage without a conversation first.
- **Raiser's Edge data must NEVER reach an LLM** (Charles, 2026-06-10 — highly sensitive donor data). Stage 7 RE matching is deterministic formulas only, expressed as certainty tiers `Match` > `Probable` > `Potential`. The normative tier rules live in `cchq-orchestrator/references/schema_contract.md` — don't restate them elsewhere.

## House rules

- Don't make structural decisions silently. If something in the scope doc looks wrong or under-specified, push back before coding, not after.
- Show running output frequently. A working thin slice is more useful than a polished sub-component held back until "done."
- Build one phase at a time, in the order suggested in the scope (or with a reason for choosing differently).
- Be opinionated and explain *why*, not just *what*. Harry's here to learn.
- When you produce intermediate files, follow the existing naming convention (`results_POSTCODE.csv`, `POSTCODE_enriched.xlsx`, `#LE1-0001` style Unique IDs) so the output is recognisable to people who've seen the current pipeline.

## Out of scope

Don't build any of these without a conversation first: replacing Apollo, replacing Raiser's Edge or VoteSource, building a CRM or persistent contact database, multi-user real-time collaboration, anything that touches Step 18 (the Treasurers' actual outreach work), or wiring any LLM into any pipeline stage (the Gemini second pass was deliberately removed 2026-06-11). See scope §8 for the authoritative list.
