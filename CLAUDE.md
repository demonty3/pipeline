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

## Standing constraints

- **Vibe-coder context.** Harry is an intern who codes with AI assistance, not a deep software engineer. Prefer simple, well-supported, maintainable choices. Conventional over clever.
- **Storage is filesystem-first.** Master tables live as CSV/Parquet/XLSX inside per-project folders. A small SQLite index for project metadata is fine; a full database is overkill at this scale.
- **Python is the default.** The existing v4 app is Python/Flask; reusing that ecosystem is the path of least resistance unless there's a strong reason to switch.
- **Schema continuity is load-bearing.** The master table keeps the existing 26 Companies House columns + Unique ID + 22 Apollo columns + `Match?` + RE-flag fields. The Treasurers' final deliverable must stay shape-compatible with the current handover — they shouldn't have to learn a new file format.
- **Apollo bulk upload caps at 10,000 rows per document.** Any batching logic has to respect this. The "batch magazine" pattern in the scope is the canonical design for this.
- **A Gemini API key is available.** Store it in `.env` as `GEMINI_API_KEY`, never commit. Use Gemini Flash as a *second-pass disambiguation tool* inside the Y/T/N classifier only — not as a general-purpose layer over the pipeline. Cascade is always: deterministic logic first → LLM only on ambiguous residual → human only on what the LLM flags low-confidence. Batch row-pairs per Gemini call to amortise overhead, require structured output (label + confidence + reason), and log every decision for audit.
- **Raiser's Edge data must NEVER reach an LLM** (Charles, 2026-06-10 — highly sensitive donor data). Stage 7 RE matching is deterministic formulas only, expressed as certainty tiers `Match` > `Probable` > `Potential`. The normative tier rules live in `cchq-orchestrator/references/schema_contract.md` — don't restate them elsewhere.

## House rules

- Don't make structural decisions silently. If something in the scope doc looks wrong or under-specified, push back before coding, not after.
- Show running output frequently. A working thin slice is more useful than a polished sub-component held back until "done."
- Build one phase at a time, in the order suggested in the scope (or with a reason for choosing differently).
- Be opinionated and explain *why*, not just *what*. Harry's here to learn.
- When you produce intermediate files, follow the existing naming convention (`results_POSTCODE.csv`, `POSTCODE_enriched.xlsx`, `#LE1-0001` style Unique IDs) so the output is recognisable to people who've seen the current pipeline.

## Out of scope

Don't build any of these without a conversation first: replacing Apollo, replacing Raiser's Edge or VoteSource, building a CRM or persistent contact database, multi-user real-time collaboration, anything that touches Step 18 (the Treasurers' actual outreach work), or wiring Gemini calls into stages outside the Y/T/N and RE disambiguation tiers. See scope §8 for the authoritative list.
