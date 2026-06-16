---
up: "[[Pipeline]]"
---

# Claude Code kickoff prompt — CCHQ Business Club Building

Paste everything below the line into a fresh Claude Code session opened in the project folder.

---

I'm rebuilding an internal data-pipeline tool for CloudMundi. The team currently runs an 18-step manual process to turn Companies House postcode searches into a ranked list of contactable prospects for CCHQ donor-outreach events. The rebuild collapses that into a small project-orchestrator web app.

The full concept is written up in this folder. **Read these four files in this order before doing anything else:**

1. **`CCHQ Project Scope - Concept v1.docx`** — the scope document. Defines the 8 orchestrator stages, 4 key features (Apollo batch magazine, Y/T/N auto-classifier, RE fuzzy flagger, async VS pause/resume), the data model concept, and what's explicitly out of scope. This is the authoritative brief.
2. **`CCHQ - Project Flow.docx`** — the original 18-step manual process, for context on what we're replacing.
3. **`App_Documentation_v4.docx`** — the spec for the existing Flask app. It covers Stages 1–2 (Companies House postcode search + CSV name enrichment) and is a reasonable reference implementation for those stages, *not* a starting codebase. The rebuild is a rebuild, not a fork — reuse patterns where they help, ignore where they don't.
4. **`Leicester - Data v1.xlsx`** and **`Leicester - Data v3.xlsx`** — real input/output examples for one event. `Data v1` Sheet1 shows the master-table schema we have to preserve (26 Companies House columns + Unique ID + sanity-check column + 22 Apollo columns). `Data v3` shows the final multi-tab deliverable shape (ALL / Y&T / Potential RE Match).

## What I want you to do first

Do not write code yet. Instead:

1. **Read the four files above.** Skim the Excel — you just need the column structure and rough row counts, not every value.
2. **Propose a tech stack** with a brief one-paragraph rationale. My constraints: I'm a vibe coder, not a software engineer, so prefer simple, well-supported, easy-to-maintain choices. The existing app is Python + Flask, so Python is the path of least resistance unless you have a strong reason to switch. Storage should be filesystem-first — the master tables are CSV/Parquet/XLSX per project folder, not rows in a database. A tiny SQLite index for project metadata is fine; full DB is overkill. A **Gemini API key is available** (in `.env` as `GEMINI_API_KEY`) — `CLAUDE.md` and the scope doc explain where it's allowed to be used (Y/T/N and RE disambiguation only, as a second-pass tier after deterministic logic). Don't sprinkle Gemini calls elsewhere.
3. **Propose a phased build plan**, ordered by value-per-effort. The scope doc suggests roughly:
   - Phase 1: Companies House fetch + regional merge + SIC industry mapping (Stages 1–2). Replaces the v4 Flask app cleanly.
   - Phase 2: Apollo batch magazine + Y/T/N auto-classification assistant (Stages 3–5). Highest day-to-day productivity gain.
   - Phase 3: Raiser's Edge fuzzy flagger + async VS pause/resume (Stages 6–7) + final deliverable export (Stage 8).
   - If you'd order it differently, say why.
4. **For each phase, describe what "done" looks like** — concrete enough that we'll both know when it's shipped.
5. **Wait for my approval** before writing any code.

## When you do start building

Work one phase at a time. Show me running output frequently — a working thin slice is more useful than a polished sub-component. Don't dump 800 lines on me at once; build incrementally and let me poke at it as it grows. If a design decision matters, ask before committing to it.

## Tone

Be opinionated. If something in the scope doc looks wrong, under-specified, or solvable in a smarter way than I described, push back before coding rather than implementing what I asked for and discovering later that it doesn't work. I'm an intern — I'm here to learn — so explain rationale, not just choices.

## Quick reference for the numbers (Leicester baseline)

- 53,048 non-blank master rows after Companies House + regional merge
- 49,377 unique persons by (surname, first name, DOB) — dedup saves ~3,700 redundant Apollo lookups
- 14% Apollo hit rate (7,244 matched)
- Apollo bulk CSV upload caps at **10,000 rows per document** — the master has to be auto-split into batches and re-stitched by Unique ID
- Y/T/N split (post-Apollo sanity check): 37% Yes / 44% Tentative / 19% No
- 5,784 rows reach the Y&T tab in the final deliverable; 57 are pre-flagged as Potential RE matches
- Raiser's Edge name list for Leicester: 1,881 entries, mixing person and company names in one column

These exist to anchor your design choices — they're not contractual.

---
