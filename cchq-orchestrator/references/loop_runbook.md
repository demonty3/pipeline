# Background loop runbook — one tick

The background loop runs this procedure on each wake-up. It polls Gmail (read
access only) for new pipeline inputs, routes them, advances the pipeline as far
as the deterministic + Gemini stages allow, and records state locally. Keep each
tick cheap and idempotent — most ticks will find nothing new and exit fast.

State files (under `cchq-orchestrator/state/`):
- `processed_threads.json` — `processed_thread_ids` (Gmail) and `processed_file_ids`
  (Drive intake folder) already handled. Skip these.
- `status.md` — append-only human-readable log of what each tick did and any
  checkpoint that needs a person (since we can't email — see connector note).

## Per-tick steps

1. **Poll two sources.**
   - **Drive intake folder (data files).** `search_files` for new CSV/XLSX in the
     "CCHQ Pipeline Inbox" folder; skip ids already in `processed_file_ids`.
   - **Gmail (triggers/signals).** `search_threads` with
     `from:c.ames@cloudmundi.com newer_than:3d (Apollo OR VoteSource OR "Raiser" OR postcode OR results OR enriched OR has:drive)`;
     drop thread ids already in `processed_thread_ids`.
   If both come up empty, append a one-line "nothing new" entry to `status.md`
   and finish the tick.

2. **Read & identify.** Data files come from the **Drive intake folder**, not
   email attachments (the connector can't download attachment bytes — see
   connector note). For Gmail triggers, `get_thread` (FULL_CONTENT). For each new
   file/thread decide what it carries by content — not filename (Apollo exports
   are random-hash-named) — using `schema_contract.md`:
   - Drive-linked data file → note the Drive file id from the link and pull it
     with the Drive MCP (`download_file_content` / `read_file_content`).
   - Apollo enriched CSV (22 Apollo cols) → Stage 4 `ingest`.
   - VoteSource return (`Unique ID`/`UniqueID` col) → Stage 6 `vs-return`.
   - Raiser's Edge name list → Stage 7 `re-flag`.
   - Companies House `results_*.csv` → drop in `postcodes/`, then Stage 2 `merge`.
   - A bare API key (`AIza...`) → write to `app/.env`, don't run a stage.

3. **Run the stage.** Call `scripts/run_stage.py --region <RC> <command> …`.
   Read the `OK <stage>` line. Stages 5 and 7 are fully autonomous (no human
   gate) — Gemini's call is auto-accepted and logged; report the Y/T/N or
   flag counts.

4. **Record.** Add the thread id to `processed_threads.json`. Append to
   `status.md`: timestamp, what was processed, the stage result, and any
   checkpoint a person must act on (Apollo upload, VoteSource send, or "review
   the auto-resolved audit CSV if you want to spot-check").

5. **Stop conditions for the tick.** Stage 1 (`fetch`) needs `CH_API_KEY`;
   Stages 5/7 need `GEMINI_API_KEY`. If a needed key is missing, log it to
   `status.md` and move on — don't crash the loop.

## Connector note (current Gmail scopes are read-only)

`search_threads` / `get_thread` / `list_labels` work. `create_label`,
`label_thread`, and `create_draft` return "insufficient authentication scopes",
so the loop cannot label threads or send mail. Also, `get_thread` returns
attachments as metadata only (`filename`/`id`/`mimeType`) — there is no tool to
download attachment bytes, so inbound data files come from the **Drive intake
folder** ("CCHQ Pipeline Inbox"), read via the Drive MCP, rather than from email.
Until write scope is granted:
- **Idempotency** uses `processed_threads.json`, not `CCHQ/*` labels.
- **Status / deliverable hand-off** is written to `status.md` (and the
  deliverable XLSX sits in the project folder) instead of being emailed.
When write scope is added, switch to the label board and `create_draft` flow in
`gmail_playbook.md` — the role logic there is unchanged.
