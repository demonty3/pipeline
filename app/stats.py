"""
Per-stage headline numbers for the project page stats strip.

Everything is derived from the most-advanced master CSV (read once, selected
columns only) plus the per-area results CSVs and the apollo_batches table.
File-derived stats are cached by (path, mtime, size) so page loads stay fast
even with 20k-row masters — the cache invalidates itself the moment any
stage rewrites a file.
"""
import os
import pandas as pd

from stages.exporter import MASTER_SUFFIXES, VS_RETURN_COLS

# The only master columns the strip needs — keeps the read cheap.
_MASTER_COLS = {
    "Unique ID", "Apollo Duplicate", "Apollo First Name", "Apollo Email",
    "Result", "RE Match?", "Potential",
} | set(VS_RETURN_COLS)

_cache = {}  # project_id -> (files_key, stats)


def _file_key(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return (path, None, None)


def project_stats(project, pdir, batches):
    """Return {'s1': {...} | None, ..., 's8': {...} | None} for the strip."""
    rc = project["region_code"]
    area_paths = [
        os.path.join(pdir, "postcodes", f"results_{a.replace(' ', '').upper()}.csv")
        for a in project["postcodes"]
    ]
    master_path = None
    for suffix in MASTER_SUFFIXES:
        p = os.path.join(pdir, f"master_{rc}_{suffix}.csv")
        if os.path.exists(p):
            master_path = p
            break

    key = tuple(_file_key(p) for p in area_paths + ([master_path] if master_path else []))
    cached = _cache.get(project["id"])
    if cached and cached[0] == key:
        stats = dict(cached[1])
    else:
        stats = _file_stats(area_paths, master_path)
        _cache[project["id"]] = (key, dict(stats))

    # Stage 3 comes from the DB — cheap, always fresh.
    if batches:
        stats["s3"] = {
            "rows": sum(int(b.get("row_count") or 0) for b in batches),
            "batches": len(batches),
            "sent": sum(1 for b in batches if b["status"] != "pending"),
        }
    else:
        stats["s3"] = None
    return stats


def _file_stats(area_paths, master_path):
    s = {f"s{i}": None for i in (1, 2, 4, 5, 6, 7, 8)}

    # ── Stage 1: officer rows fetched per area ────────────────────────────────
    done = [p for p in area_paths if os.path.exists(p)]
    if done:
        rows = 0
        for p in done:
            try:
                rows += len(pd.read_csv(p, usecols=[0], dtype=str, keep_default_na=False))
            except Exception:
                pass
        s["s1"] = {"rows": rows, "areas_done": len(done), "areas_total": len(area_paths)}

    if not master_path:
        return s
    try:
        m = pd.read_csv(master_path, dtype=str, keep_default_na=False,
                        usecols=lambda c: c in _MASTER_COLS)
    except Exception:
        return s

    n = len(m)

    def col(name):
        return m[name].astype(str).str.strip() if name in m.columns else None

    # ── Stage 2: master size + duplicate persons flagged ──────────────────────
    dup = col("Apollo Duplicate")
    s["s2"] = {"rows": n, "dups": int((dup == "True").sum()) if dup is not None else 0}

    # ── Stage 4: Apollo enrichment present / usable emails ────────────────────
    first, email = col("Apollo First Name"), col("Apollo Email")
    if first is not None or email is not None:
        matched = pd.Series(False, index=m.index)
        if first is not None:
            matched |= first != ""
        if email is not None:
            matched |= email != ""
        s["s4"] = {"matched": int(matched.sum()),
                   "emails": int((email != "").sum()) if email is not None else 0}

    # ── Stage 5: sanity-check verdicts ─────────────────────────────────────────
    res = col("Result")
    if res is not None:
        s["s5"] = {"y": int((res == "Y").sum()),
                   "t": int((res == "T").sum()),
                   "n": int((res == "N").sum())}

    # ── Stage 6: rows that came back with VoteSource data ──────────────────────
    vs_cols = [c for c in VS_RETURN_COLS if c in m.columns]
    if vs_cols:
        overlaid = (m[vs_cols].apply(lambda c: c.astype(str).str.strip()) != "").any(axis=1)
        s["s6"] = {"overlaid": int(overlaid.sum())}

    # ── Stage 7: RE flags by certainty tier ────────────────────────────────────
    reflag, tier = col("RE Match?"), col("Potential")
    re_mask = (reflag == "Y") if reflag is not None else pd.Series(False, index=m.index)
    if reflag is not None:
        tiers = tier[re_mask].value_counts() if tier is not None else {}
        s["s7"] = {"flagged": int(re_mask.sum()),
                   "match": int(tiers.get("Match", 0)),
                   "probable": int(tiers.get("Probable", 0)),
                   "potential": int(tiers.get("Potential", 0))}

    # ── Stage 8: deliverable tab sizes (same masks as the exporter) ────────────
    if res is not None:
        yt = res.isin(["Y", "T"]) & ~re_mask
        s["s8"] = {"all": n, "yt": int(yt.sum()), "re": int(re_mask.sum())}
    return s
