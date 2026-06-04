# CCHQ Pipeline — Automation & Handoff Layer
### PRD & Implementation Plan · v1 · 2026-06-04

> Scope: the **agentic automation layer** on top of the existing 8-stage CCHQ
> orchestrator — the headless runner, the Drive/Gmail intake loop, and packaging
> it for **Charles to run personally**. This complements (does not replace) the
> product scope in `CCHQ Project Scope - Concept v1.docx` and the format
> contract in `cchq-orchestrator/references/schema_contract.md`.

---

## 1. Problem & goal

The 8-stage pipeline works but is driven by hand, one command at a time, with a
person shuttling files between Apollo / VoteSource / Raiser's Edge web UIs and
the master sheet. The goal of this layer is to **remove the babysitting**: let
inbound files (Apollo exports, VoteSource returns, RE lists) be collected and
routed automatically, advance the pipeline as far as the deterministic + Gemini
stages allow, and stop cleanly at the genuine human checkpoints — all runnable
by Charles without a developer present.

**Not the goal:** making Apollo/VoteSource themselves faster (they're external,
manual web-UI steps and explicitly out of scope), replacing any vendor system,
or wiring Gemini outside the Stage 5/7 disambiguation tiers.

## 2. Users

- **Charles Ames** — primary operator post-handoff. Runs the pipeline, uploads
  to Apollo/VoteSource, drops returned files into the Drive folder, sends the
  final deliverable. Not a developer.
- **Harry** — maintainer. Extends/fixes the pipeline with AI assistance;
  favours simple, conventional, maintainable choices.

## 3. Current state (validated 2026-06-04)

Built and working:
- **Headless runner** `cchq-orchestrator/scripts/run_stage.py` — one command per
  stage, file-in/file-out, reuses the app's SQLite index + project folders. Ends
  each run with `OK <stage>`.
- **The skill** `cchq-orchestrator/` (SKILL.md + references + scripts) sequences
  stages and validates formats between them.
- **Gmail read connector** — `search_threads`/`get_thread` confirmed live.
- **Stages 1–5 + 8 validated end-to-end** on real SW1 data and a synthetic
  Apollo export: schema contract holds (30-col master, 56-col/3-tab deliverable),
  ingest matched 1000/1000 by Unique ID, classify produced 999/1/539 incl. a
  live Gemini Pass-2 call, deliverable Y&T tab populated correctly.
- **Merge idempotency guard** — re-running `merge` is blocked (exit 2) unless
  `--force`; prevents Unique-ID reassignment that would orphan Stage 5/7 decisions.

Not yet validated (needs real inputs from Charles): Stage 6 `vs-return`, Stage 7
`re-flag`, ingest against a *real* Apollo export, and the `clean_company_name`
golden regression test (needs the SW3 files).

## 4. Key decisions & constraints

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **File transport = a Google Drive intake folder** (`CCHQ Pipeline Inbox`), not email | The Gmail connector returns attachment metadata only (no byte download), and the Apollo export comes from the vendor — it can't be reshaped into a "link from Charles". Drive MCP can read a folder. |
| D2 | **Identify files by column schema, never filename** | Apollo exports are random-hash-named (`a3fb8aaa….csv`). |
| D3 | **Gmail used for triggers/signals only** | Read-only scopes today; file transport moved to Drive. |
| D4 | **Stages 5 & 7 run autonomously** (Gemini auto-accepted, every decision logged) | Throughput; audit trail in `classifications_log.csv` + `stage{5,7}_autoresolved_<RC>.csv` keeps it safe. |
| D5 | **Schema continuity is load-bearing** | Deliverable must stay shape-compatible with `Leicester - Data v3.xlsx`. |
| D6 | **Charles's primary surface = the Claude Code agent + loop; Flask app = review/eyeball; CLI = engine** | Only the agent surface delivers the automation (intake, notifications, autonomous stages). Requires Gmail+Drive connectors authed in Charles's environment. |

## 5. Requirements

### Functional
- **F1 — Drive intake.** Poll the `CCHQ Pipeline Inbox` Drive folder; download new
  CSV/XLSX via the Drive MCP; skip files already processed.
- **F2 — Schema routing.** Classify each inbound file by header inspection →
  Apollo enriched (22 Apollo cols) → Stage 4; `Unique ID`/`UniqueID` col →
  Stage 6; RE name list → Stage 7; `results_*.csv` → Stage 1/2.
- **F3 — Idempotent loop.** One tick = poll → identify → run stage → record.
  Track `processed_file_ids` (Drive) and `processed_thread_ids` (Gmail) in
  `state/processed_threads.json`; append outcomes to `state/status.md`.
- **F4 — Gmail triggers.** Parse event-request emails (region + postcodes) to
  kick off Stages 1–3.
- **F5 — Human checkpoints.** Stop and log at Apollo upload and VoteSource send;
  surface review/auto-resolved counts.
- **F6 — Handoff docs.** `For Charles - Quickstart.md` + the runbooks, accurate
  and self-serve.
- **F7 (future) — Send-back.** With Gmail write scope: draft + send the
  deliverable (confirm-before-send), label board for idempotency.

### Non-functional
- **Maintainability** — conventional Python, thin shell over existing stages, no
  new framework.
- **Security/PII** — donor PII; keys in `app/.env` (never committed); `state/`
  and project folders gitignored; trust only `c.ames@cloudmundi.com` as sender.
- **Observability** — every stage prints progress + `OK`; every Gemini decision
  logged; loop writes a status line each tick.
- **Idempotency** — re-processing a file/thread is a no-op; re-running `merge` is
  guarded.

## 6. Risks & open questions

| Risk / question | Status / mitigation |
|---|---|
| Drive connector must be signed into the account owning `CCHQ Pipeline Inbox` | **Confirm before handoff** — else the folder poll sees nothing. |
| Gmail write scope not granted → can't auto-send deliverable | Falls back to `state/draft_<RC>.md` + file in folder. Decide if worth re-auth. |
| Real Apollo export columns may differ from synthetic test | Validate F2/Stage 4 against a real export (needs Charles). |
| Stages 6 & 7 untested with real data | Validate `vs-return` + `re-flag` with a real VS return + RE export. |
| `clean_company_name` dropped `llc` vs Charles's spec | Near-zero impact (UK data); decide whether to re-add. |
| Background loop in headless/cron runs may lack interactively-authed MCP | Use interactive `/loop`; document the limitation. |

## 7. Implementation plan

### Phase 0 — Hardening (DONE, 2026-06-04)
- Merge idempotency guard + `--force`; VS-return dedup + existence guard;
  `stage_summary` reuse of `_pick_master`; schema-contract `Match?`/`Result` fix.
- Docs corrected to the Drive-intake-folder model; quickstart written.
- Validation pass on SW1 + synthetic E2E (§3).

### Phase 1 — Drive intake folder (the core of this layer)
1. Create the `CCHQ Pipeline Inbox` Drive folder; confirm the Drive connector
   account owns/can read it.
2. Loop tick: `search_files` scoped to that folder → list new CSV/XLSX →
   `download_file_content` → save into the project folder.
3. Schema-classify each file (F2) and run the matching stage.
4. Record `processed_file_ids` + append to `status.md`.
- **Acceptance:** drop a known file in the folder → correct stage runs → second
  tick is a no-op. *(Test with a synthetic file first, then a real one.)*

### Phase 2 — Validate the untested stages (needs Charles's data)
1. Real Apollo export → confirm Stage 4 ingest match rate & column mapping.
2. Real VoteSource return → `vs-return` folds columns without row explosion.
3. Real RE export → `re-flag` flags potential matches; deliverable RE tab fills.
4. SW3 golden files → run `test_clean_company_name.py` to green.
- **Acceptance:** a full LE-style project runs 1→8 producing a v3-shaped
  deliverable; diff Stage 8 vs a golden file.

### Phase 3 — Background loop productionisation
1. Finalise the `/loop` invocation + cadence (30m–1h).
2. Error handling: missing key / bad file / stage failure logs to `status.md`
   and continues (never crashes the loop).
3. Checkpoint nudges written to `status.md` at Apollo/VoteSource steps.
- **Acceptance:** loop runs unattended across several ticks, processes a file
  when it lands, idles cheaply otherwise.

### Phase 4 — Send-back (optional / future)
1. Re-authorise Gmail with compose/modify scope.
2. Switch from `state/` hand-off to `create_draft` (confirm-before-send) + the
   `CCHQ/*` label board.
- **Acceptance:** deliverable drafts to the requester (cc Charles) with the
  generated summary; nothing sends without confirmation.

## 8. Success metrics
- Charles runs a region end-to-end from the quickstart with **no developer help**.
- Inbound files are auto-routed correctly (right stage, by schema) ≥ 99% of the time.
- Zero schema-contract regressions (Stage 8 stays v3-shape-compatible).
- Time from "Apollo export lands" → "ingested" drops from manual handling to one loop tick.

## 9. Sequencing
**Now:** Phase 1 (Drive intake) — unblocked, the highest-leverage piece.
**When Charles can share data:** Phase 2 (validate 6/7 + real Apollo + SW3).
**Then:** Phase 3 (loop hardening). **Later, if wanted:** Phase 4 (send-back).
