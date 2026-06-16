"""
Project merge — combine two or more projects into one NEW project.

What it does, in plain English:
  Sometimes an event covers ground that was fetched as separate projects
  (two postcode areas run on different days, a late-added district). This
  takes the masters of the selected projects and stacks them into a fresh
  project, so the remaining stages — and the final deliverable — run once
  over the whole list, the way Leicester ran #LE1–#LE5 as one file.

  The rules that keep it safe:
    - The sources are never touched. The merge only ever CREATES a new
      project; deleting it is a full undo.
    - Rows keep their original Unique IDs. UIDs are the join key for
      everything downstream (Apollo enrichment, classifications, RE
      flags), so reminting them would orphan all of that work. If two
      sources share a UID (two projects with the same region prefix),
      the merge refuses outright rather than guess which row is which.
    - We merge at the deepest stage ALL sources have reached (their
      most-complete common master file). Later-stage work that only one
      source has is not carried over half-populated — those stages are
      simply re-run on the merged project, which is cheap for every
      stage that isn't Apollo, and Apollo (Stage 3-4) survives because
      every master file builds on the one before it.
    - Stage 5/7 decision audit trails and the per-identity UID maps come
      along, so the review queue and stable-UID re-merge logic keep
      working on the merged project.
"""
import json
import os

import pandas as pd

from stages.exporter import MASTER_SUFFIXES   # most → least complete
from stages.merge import _uid_map_path

# Master suffix → the highest pipeline stage that master reflects.
# Stages 1..N are marked complete on the merged project; the rest stay pending.
SUFFIX_STAGE = {"raw": 2, "enriched": 4, "classified": 5, "vs": 6, "re_flagged": 7}


def _existing_suffixes(project_dir, region_code):
    return {s for s in MASTER_SUFFIXES
            if os.path.exists(os.path.join(project_dir, f"master_{region_code}_{s}.csv"))}


def _common_suffix(sources_with_dirs):
    """The most-complete master suffix that exists in EVERY source."""
    common = None
    for suffix in MASTER_SUFFIXES:   # ordered most → least complete
        if all(suffix in sfx for _, _, sfx in sources_with_dirs):
            common = suffix
            break
    return common


def merge_projects(sources, db, new_name, new_region_code, progress_cb=None):
    """
    sources: list of (project_dict, project_dir) for the projects to merge.
    Creates the new project (DB row + folder + merged master) and returns
    {"project_id", "project_dir", "rows", "suffix", "stage", "dup_people"}.

    Raises ValueError with an operator-readable message on any refusal —
    nothing has been created yet at that point.
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)

    # ── Validate sources before creating anything ─────────────────────────────
    triples = []
    for project, pdir in sources:
        sfx = _existing_suffixes(pdir, project["region_code"])
        if not sfx:
            raise ValueError(f"'{project['name']}' has no master file yet — "
                             f"run it through Stage 2 before merging.")
        triples.append((project, pdir, sfx))

    suffix = _common_suffix(triples)
    if suffix is None:
        raise ValueError("The selected projects have no master stage in common.")
    stage_reached = SUFFIX_STAGE[suffix]

    frames = []
    for project, pdir, _ in triples:
        path = os.path.join(pdir, f"master_{project['region_code']}_{suffix}.csv")
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        frames.append((project, df))
        log(f"  {project['name']}: {len(df):,} rows ({os.path.basename(path)})")

    # UID collision = hard refusal. A shared UID means two different people
    # would become indistinguishable to every UID-keyed join downstream.
    seen = {}
    for project, df in frames:
        uids = set(df["Unique ID"].astype(str)) - {""}
        for other_name, other_uids in seen.items():
            clash = uids & other_uids
            if clash:
                example = sorted(clash)[0]
                raise ValueError(
                    f"'{project['name']}' and '{other_name}' share {len(clash):,} "
                    f"Unique ID(s) (e.g. {example}) — probably the same region "
                    f"prefix. Merging would mis-join their data; these projects "
                    f"can't be merged automatically.")
        seen[project["name"]] = uids

    merged = pd.concat([df for _, df in frames], ignore_index=True).fillna("")

    # Same person appearing in several sources (overlapping search areas) is
    # legal — different UIDs, real rows — but the operator should know the
    # outreach list now holds duplicates. Detect with Stage 2's identity key.
    ident = merged.apply(lambda r: "|".join(str(r.get(c, "")).strip().lower()
                                            for c in ("Surname", "First Name",
                                                      "Officer date of birth",
                                                      "Company Number")), axis=1)
    dup_people = int(ident.duplicated().sum())
    if dup_people:
        log(f"  Heads-up: {dup_people:,} row(s) look like the same person+company "
            f"appearing in more than one source project (overlapping search areas?). "
            f"They are kept — review before outreach.")

    # ── Create the new project ────────────────────────────────────────────────
    postcodes = []
    for project, _, _ in triples:
        for pc in project["postcodes"]:
            if pc not in postcodes:
                postcodes.append(pc)

    new_id = db.create_project(new_name, new_region_code, "", postcodes)
    new_dir = db.project_dir(new_id, new_region_code)
    os.makedirs(os.path.join(new_dir, "postcodes"), exist_ok=True)

    merged.to_csv(os.path.join(new_dir, f"master_{new_region_code}_{suffix}.csv"),
                  index=False)

    # Union the identity→UID maps and carry the highest counter, so a future
    # Stage 2 run on the merged project reuses existing UIDs for known people
    # and can never mint a number that collides with a carried-over one.
    uid_map, counter = {}, 0
    for project, pdir, _ in triples:
        p = _uid_map_path(pdir, project["region_code"])
        if os.path.exists(p):
            with open(p) as fh:
                d = json.load(fh)
            uid_map.update(d.get("map", {}))
            counter = max(counter, int(d.get("counter", 0)))
        counter = max(counter, int(project.get("unique_id_counter") or 0))
    if uid_map:
        with open(_uid_map_path(new_dir, new_region_code), "w") as fh:
            json.dump({"counter": counter, "map": uid_map}, fh)
    db.set_unique_id_counter(new_id, counter)

    # Stage statuses: complete up to the merge point, pending after it.
    for n in range(1, 9):
        getattr(db, f"update_stage{n}_status")(new_id, "complete" if n <= stage_reached
                                               else "pending")
    # A re_flagged merge can exist without Stage 6 having run (VS is skippable);
    # only claim VoteSource is done if EVERY source actually completed it —
    # otherwise mark it skipped so the UI doesn't imply VS data exists.
    if stage_reached >= 7 and not all(p["stage6_status"] == "complete"
                                      for p, _, _ in triples):
        db.mark_stage_skipped(new_id, 6)

    # Decision audit trails + the Stage 5 log travel with their rows.
    if stage_reached >= 5:
        logs = []
        for project, pdir, _ in triples:
            db.copy_s5_decisions(project["id"], new_id)
            lp = os.path.join(pdir, "classifications_log.csv")
            if os.path.exists(lp):
                logs.append(pd.read_csv(lp, dtype=str, keep_default_na=False))
        if logs:
            pd.concat(logs, ignore_index=True).fillna("").to_csv(
                os.path.join(new_dir, "classifications_log.csv"), index=False)
    if stage_reached >= 7:
        for project, _, _ in triples:
            db.copy_s7_decisions(project["id"], new_id)

    # Provenance — the merged project's log says exactly what went into it.
    source_desc = " + ".join(f"'{p['name']}' ({len(df):,} rows)"
                             for (p, _, _), (_, df) in zip(triples, frames))
    db.add_log(new_id, 2, f"Created by merging {source_desc} at the "
                          f"'{suffix}' stage (Stage {stage_reached}). "
                          f"Source projects were not modified.")
    log(f"  Merged project created: {len(merged):,} rows at '{suffix}' "
        f"(through Stage {stage_reached})")

    return {"project_id": new_id, "project_dir": new_dir, "rows": len(merged),
            "suffix": suffix, "stage": stage_reached, "dup_people": dup_people}
