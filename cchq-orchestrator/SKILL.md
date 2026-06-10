---
name: cchq-orchestrator
description: >-
  Run the CCHQ Business Club donor-list pipeline end to end — the 8
  orchestration stages that turn Companies House postcode searches into a
  ranked, Apollo-enriched, RE/VoteSource-overlaid deliverable for CloudMundi.
  Use when asked to build/advance a CCHQ or "Business Club" list, run a pipeline
  stage (Companies House fetch, merge, Apollo magazine/ingest, Y/T/N classify,
  VoteSource export, Raiser's Edge flag, final deliverable), or process pipeline
  inputs/outputs over Gmail. Region codes look like "LE1"; Unique IDs like
  "#LE1-0001".
---

# CCHQ pipeline orchestrator

Drives the 8-stage CCHQ pipeline by calling the existing engine in `app/stages/`
through `scripts/run_stage.py`. The skill's job is **sequencing, format
validation between stages, and Gmail intake/output** — not re-implementing
pipeline logic. The deterministic Python does the heavy lifting; Gemini is a
second-pass tier inside Stages 5 and 7 only.

## Before anything: the format contract is load-bearing

Every stage adds columns to one master table keyed by `Unique ID`, without
re-ordering rows. If column names/order drift, downstream joins mis-merge and
the Treasurers' deliverable stops matching the format they expect. **Read
`references/schema_contract.md` before running merge, ingest, or export, and
before touching any column.** It is reconciled against the real golden files
(`Leicester - Data v1/v3.xlsx`) and is the source of truth.

## The 8 stages

| # | Stage | `run_stage.py` command | Produces |
|---|-------|------------------------|----------|
| 1 | Companies House fetch | `fetch --area <PC>` | `postcodes/results_<PC>.csv` |
| 2 | Regional merge + Unique IDs + SIC | `merge` | `master_<RC>_raw.csv` |
| 3 | Apollo batch magazine (≤10k/file) | `magazine [--budget N]` | `apollo_batch_NNN.csv` |
| 4 | Apollo ingest (re-stitch by Unique ID) | `ingest --files …` | `master_<RC>_enriched.csv` |
| 5 | Y/T/N classifier (det.→Gemini→human) | `classify` | `master_<RC>_classified.csv` |
| 6 | VoteSource export + async return | `vs-export` / `vs-return --file` | `vs_export_<RC>.xlsx` / `master_<RC>_vs.csv` |
| 7 | Raiser's Edge fuzzy flagger | `re-flag --file <re.csv>` | `master_<RC>_re_flagged.csv` |
| 8 | Final multi-tab deliverable | `export` | `<RC>_final_deliverable.xlsx` |

Stages 3 (Apollo upload) and 6 (VoteSource send) have **human/web-UI steps in
the middle** — the magazine builds the upload, a person runs it through Apollo's
or VoteSource's own UI, and the result comes back for `ingest` / `vs-return`
(often over Gmail — see below). Stage 5 may leave a residual band for
human review; the runner reports the count. Stage 7 never does — it is a
single deterministic pass with no review queue.

## How to run a stage

```bash
cd app && python ../cchq-orchestrator/scripts/run_stage.py --region LE1 <command> [opts]
```

The runner reuses the app's own `projects.db` and `app/projects/<id>_<RC>/`
folder, so a run is the *same* project the Flask app would show. Each command
prints the stage's progress and ends with `OK <stage>` on success. Always:

1. `run_stage.py --region <RC> status` — see which stages are done and which
   `master_<RC>_*.csv` exist.
2. Confirm the input file for the stage you're about to run carries the columns
   the previous stage should have added (per `schema_contract.md`).
3. Run the stage. Read the `OK` line and any review counts before advancing.

**Keys:** Stage 1 needs `CH_API_KEY`, Stage 5 needs `GEMINI_API_KEY`. Put
them in `app/.env` (never commit — see `app/.env.example`). The runner loads
`app/.env` automatically. Stage 7 needs no key — RE matching is deterministic
formulas only; RE donor data must never reach an LLM (Charles, 2026-06-10).

## Gmail (agentic / background)

The pipeline can ingest inputs and push outputs over the Gmail MCP connector so
runs progress without babysitting the web app — receiving Apollo/VoteSource/RE
files, triggering runs from an event-request email, sending the deliverable, and
posting status/review nudges. **Read `references/gmail_playbook.md`** for the
file→stage mapping and the confirm-before-send rule. For background operation,
the loop procedure is in `references/loop_runbook.md` (one tick: poll → identify
by schema → run stage → record state); pair it with the `/loop` skill.

**Note:** the Gmail connector is currently read-only (search/get/list work;
label/draft/send do not). The loop uses `state/processed_threads.json` for
idempotency and `state/status.md` for hand-off instead of labels/email until
compose scopes are granted. See the connector note in both reference files.

## Autonomous verification (no human gate)

Stages 5 and 7 run fully autonomously — no human review queue blocks the
pipeline. Stage 5: the deterministic pass handles the clear cases, Gemini Flash
judges the ambiguous band, and **Gemini's call is auto-accepted for every
row**; low-confidence auto-accepted rows are dumped to
`stage5_autoresolved_<RC>.csv` for after-the-fact spot-checks. Stage 7 is pure
formulas: every hit is flagged with a certainty tier in `Potential`
(`Match` > `Probable` > `Potential` — rules in
`references/schema_contract.md`), erring toward over-flagging. Every
decision (label, confidence, reason, the names compared) is logged to
`classifications_log.csv` and the project DB. This is a deliberate
accuracy/throughput trade — the audit trail is what keeps it safe.

## House rules (from the project's CLAUDE.md)

- Don't make structural decisions silently; push back before coding, not after.
- Storage is filesystem-first; the SQLite index is metadata only.
- Gemini is allowed **only** in the Stage 5 disambiguation tier — never as a
  general layer over the pipeline, and NEVER on Stage 7: Raiser's Edge donor
  data must not reach any LLM (Charles, 2026-06-10).
- Keep the existing naming conventions (`results_<PC>.csv`,
  `master_<RC>_*.csv`, `#LE1-0001` Unique IDs) so output stays recognisable.
- Schema continuity is non-negotiable — the deliverable must stay
  shape-compatible with the Treasurers' current handover.
