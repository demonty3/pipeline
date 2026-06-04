# Gmail playbook — running the pipeline from the inbox

The orchestrator is agentic: it can pick up inputs and push outputs over Gmail
(via the Gmail MCP connector) so a run can progress in the background without
someone babysitting the web app. Four roles, all optional — only do the ones
the situation calls for.

> **Authorisation:** sending email and moving files are outward-facing,
> hard-to-reverse actions. Draft first and confirm with Harry before sending
> anything to a colleague/Treasurer unless he's said "send without asking" for
> this run. Inbound reads and label changes are safe to do unprompted.

> **⚠ Current connector scopes are READ-ONLY.** As wired today,
> `search_threads` / `get_thread` / `list_labels` work, but `create_label`,
> `label_thread`, and `create_draft` fail with "insufficient authentication
> scopes". So Roles 1–4 below describe the full design, but until write scope is
> granted: idempotency uses `state/processed_threads.json` (not the `CCHQ/*`
> labels), and status/deliverable hand-off is written to `state/status.md` with
> the file left in the project folder (not emailed). To enable the label + send
> flow, re-authorise the Gmail connector with compose/modify scopes.

The pipeline's known counterpart is **Charles Ames** (`c.ames@cloudmundi.com`) —
he sent the source docs, the golden `Leicester` files, and the Gemini key, and
he shares the canonical schema. Treat mail from him as authoritative input.

## Labels (create once)

Use Gmail labels as the run's state board so background pickups are idempotent:

- `CCHQ/inbound` — emails carrying input files still to process
- `CCHQ/processed` — inputs already folded into a master
- `CCHQ/needs-review` — a stage left rows in the human-review band
- `CCHQ/delivered` — final deliverable sent

---

## Role 1 — Receive input files (inbound attachments → project folder)

Inbound files map to specific stages by shape:

| What arrives | Stage | What to run |
|--------------|-------|-------------|
| Apollo enriched CSV (22 Apollo cols) | 4 | `run_stage.py --region <RC> ingest --files <saved.csv>` |
| Returned VoteSource file (`Unique ID`/`UniqueID` col) | 6b | `run_stage.py --region <RC> vs-return --file <saved>` |
| Raiser's Edge export (name list) | 7 | `run_stage.py --region <RC> re-flag --file <saved>` |
| Companies House `results_*.csv` | 1→2 | drop into `postcodes/`, then `merge` |

Procedure:
1. `search_threads` for unprocessed inputs, e.g.
   `from:c.ames@cloudmundi.com has:attachment -label:CCHQ/processed newer_than:30d`.
2. Identify the file by the **schema_contract** (count/parse the header row to
   tell an Apollo export from a VS return from an RE list — don't guess from
   the filename alone).
3. Save the attachment into the project folder (or `postcodes/` for CH files),
   run the matching stage, confirm the `OK <stage>` line.
4. `label_thread` → `CCHQ/processed` (and remove `CCHQ/inbound`).

## Role 2 — Trigger runs (a new event request kicks off a pipeline)

A request like "we need a list for the Leicester event, postcodes LE1–LE5"
should:
1. Parse region code + postcode areas from the email body.
2. `run_stage.py --region <RC> init`, then `fetch` each area, then `merge`,
   `magazine` — i.e. drive Stages 1–3 unattended.
3. Stop at the first human checkpoint (Apollo upload is a manual web-UI step)
   and send a status email (Role 4) saying which batches are ready to upload.

## Role 3 — Send the deliverable

When Stage 8 produces `<RC>_final_deliverable.xlsx`:
1. Run `run_stage.py --region <RC> summary`. It computes the figures (rows per
   tab, Apollo hit rate, Y/T/N split, RE flags) and writes the ready-to-send
   subject+body to `state/draft_<RC>.md`.
2. With Gmail write scope: `create_draft` to the requester (cc Charles) using
   that subject/body, attach the XLSX. **Confirm with Harry, then send.** Apply
   `CCHQ/delivered`. Without write scope: the draft stays in `state/draft_<RC>.md`
   for Harry to send by hand (and `create_draft` can't attach files anyway —
   share the XLSX via Drive link in the body).

## Role 4 — Status updates / review nudges

Send a short progress note when:
- A stage completes a long unattended run (e.g. Stages 1–3 finished).
- A stage leaves rows in the human-review band — Stage 5 (`classify`) or
  Stage 7 (`re-flag`) report a non-zero review count. Label the thread
  `CCHQ/needs-review` and say exactly how many rows and where to review them.
- Something blocks (missing `CH_API_KEY`/`GEMINI_API_KEY`, Apollo credit cap
  hit and rows deferred to `apollo_deferred_<RC>.csv`).

Keep these terse and factual — counts and next action, no filler.

---

### Background-run pattern

For "do this in the background," combine with the `/loop` or `/schedule` skill:
poll `CCHQ/inbound` on an interval, process whatever landed, advance the
pipeline as far as the deterministic stages allow, and email a status note at
each human checkpoint. The labels make re-entry safe — already-processed
threads are skipped.
