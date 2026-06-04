# CCHQ pipeline — quickstart for Charles

This is the short version: how to set it up, the one rule that keeps it working,
and how to run it. The full detail lives in `cchq-orchestrator/` (the skill, the
`references/` docs, and `scripts/run_stage.py`) — you shouldn't need it day to day.

The tool turns Companies House postcode searches into a ranked, Apollo-enriched,
RE/VoteSource-overlaid donor list, in 8 stages. It runs **one stage at a time**,
file-in / file-out, and keeps the master table shape-compatible with the
`Leicester - Data v3.xlsx` handover you already know.

---

## One-time setup

1. **Install Python deps** (once):

   ```bash
   pip install -r app/requirements.txt
   ```

2. **Add your API keys** to `app/.env` (copy `app/.env.example` to start). Never
   commit this file:

   ```
   CH_API_KEY=your-companies-house-key      # Stage 1 (fetch)
   GEMINI_API_KEY=your-gemini-flash-key      # Stages 5 & 7 (classify, re-flag)
   ```

3. **Create the Drive intake folder.** Make a Google Drive folder called
   **`CCHQ Pipeline Inbox`** and share it with the Google account the assistant's
   Drive connector is signed into. This is where inbound files go (see the rule
   below).

---

## The one rule: data files go in the Drive folder

The assistant **cannot pull email attachments** (the Gmail connection is
read-only and can't download attachment bytes), and the Apollo export arrives as
an email *from Apollo*, which we can't reshape. So the workflow is:

> **When a data file comes in — an Apollo enriched export, a VoteSource return,
> a Raiser's Edge name list — drop it into the `CCHQ Pipeline Inbox` Drive
> folder.** The assistant reads it from there, works out what it is *by its
> columns* (filenames don't matter — Apollo names exports as random hashes like
> `a3fb8aaa….csv`), and runs the right stage.

Email is still useful for **triggers** ("we need a list for the Leicester event,
LE1–LE5") and status notes — just not for moving files.

---

## Running it

Every stage is one command, run from the `app/` folder:

```bash
cd app && python3 ../cchq-orchestrator/scripts/run_stage.py --region LE1 <stage> [options]
```

Each command prints its progress and ends with `OK <stage>` when it worked.
Run `status` any time to see what's done and which files exist.

| Stage | Command | What it does |
|---|---|---|
| — | `init` | create the project for this region |
| 1 | `fetch --area LE1` | Companies House search for one postcode |
| 2 | `merge` | regional merge + Unique IDs + SIC labels |
| 3 | `magazine [--budget N]` | build Apollo upload batches (≤10k rows each) |
| 4 | `ingest --files <export.csv>` | re-stitch Apollo's enriched export back in |
| 5 | `classify` | Y/T/N classifier (auto — no manual review gate) |
| 6 | `vs-export` / `vs-return --file <r>` | VoteSource upload / fold the return back in |
| 7 | `re-flag --file <re.csv>` | Raiser's Edge fuzzy match flagger |
| 8 | `export` | final multi-tab deliverable XLSX |
| — | `summary` | write the ready-to-send status/handover text |

**Two steps need you (they're web-UI steps, by design):**
- After **Stage 3**, upload the `apollo_batch_*.csv` files into Apollo yourself,
  then drop the result back in the Drive folder for Stage 4.
- After **Stage 6 `vs-export`**, run the file through VoteSource, then drop the
  return back in the folder for `vs-return`.

> **Re-running `merge` is blocked on purpose.** It would reassign every Unique ID
> and break the deliverable. If you really need to rebuild from scratch, add
> `--force` (IDs restart at `#<RC>-0001`).

---

## Hands-off mode (the background loop)

To let it run without babysitting, in Claude Code use the `/loop` skill pointed
at the runbook:

```
/loop 30m Run one tick of the cchq-orchestrator loop per cchq-orchestrator/references/loop_runbook.md: poll the CCHQ Pipeline Inbox Drive folder and Charles's email, identify each file by its column schema, run the matching stage, and append the result to state/status.md.
```

Each tick checks the Drive folder + inbox, advances the pipeline as far as the
automatic stages allow, and stops at the human checkpoints (Apollo / VoteSource)
with a note. It's safe to leave running — already-processed files are skipped.

**Heads up:** the assistant currently has *read-only* email access, so it can't
send the deliverable by email yet. Until that's granted, the finished file sits
in the project folder and a ready-to-send note is written to
`cchq-orchestrator/state/draft_<RC>.md` for you to send by hand.

---

## Where things land

- **Working files & deliverable:** `app/projects/<id>_<REGION>/` — the final file
  is `<REGION>_final_deliverable.xlsx`.
- **Audit trail:** `classifications_log.csv` (every Stage 5/7 decision) and
  `stage5/7_autoresolved_<RC>.csv` (the low-confidence calls, to spot-check).
- **Loop log & draft emails:** `cchq-orchestrator/state/`.

## If something stops
- `ERROR: CH_API_KEY / GEMINI_API_KEY is not set` → add it to `app/.env`.
- `ERROR: master_<RC>_classified.csv not found — run Stage 5 first` → you skipped
  a stage; run `status` to see where you are and run the missing one.
- Anything else → run `status`, and the assistant can read the printed log and
  tell you the next move.
