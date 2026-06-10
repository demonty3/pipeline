"""
Thin SQLite wrapper for project metadata.
The master data lives as CSV/XLSX files in per-project folders — not here.
"""
import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "projects.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                region_code TEXT NOT NULL,
                event_date TEXT,
                postcodes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                stage1_status TEXT NOT NULL DEFAULT 'pending',
                stage2_status TEXT NOT NULL DEFAULT 'pending',
                stage3_status TEXT NOT NULL DEFAULT 'pending',
                stage4_status TEXT NOT NULL DEFAULT 'pending',
                stage5_status TEXT NOT NULL DEFAULT 'pending',
                stage6_status TEXT NOT NULL DEFAULT 'pending',
                stage7_status TEXT NOT NULL DEFAULT 'pending',
                stage8_status TEXT NOT NULL DEFAULT 'pending',
                stage6_skipped INTEGER NOT NULL DEFAULT 0,
                stage7_skipped INTEGER NOT NULL DEFAULT 0,
                vs_sent_at TEXT,
                unique_id_counter INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS stage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                stage INTEGER NOT NULL,
                message TEXT NOT NULL,
                ts TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS apollo_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                batch_num INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stage5_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                unique_id TEXT NOT NULL,
                pass_num INTEGER NOT NULL,
                label TEXT NOT NULL,
                confidence REAL,
                reason TEXT,
                ts TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stage7_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                unique_id TEXT NOT NULL,
                re_name TEXT NOT NULL,
                match_type TEXT,
                label TEXT NOT NULL,
                confidence REAL,
                reason TEXT,
                pass_num INTEGER NOT NULL,
                ts TEXT NOT NULL
            );
        """)

    # Migrate existing DB: add new columns if they don't exist yet.
    _migrate()


def _migrate():
    """Add new columns to existing projects rows created before Stage 3-8 support."""
    new_cols = [
        ("stage3_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("stage4_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("stage5_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("stage6_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("stage7_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("stage8_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("stage6_skipped", "INTEGER NOT NULL DEFAULT 0"),
        ("stage7_skipped", "INTEGER NOT NULL DEFAULT 0"),
        ("vs_sent_at", "TEXT"),
    ]
    with get_db() as conn:
        for col, typedef in new_cols:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # column already exists


# ── Project CRUD ──────────────────────────────────────────────────────────────

def create_project(name, region_code, event_date, postcodes):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, region_code, event_date, postcodes, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, region_code.upper(), event_date or None, json.dumps(postcodes), now),
        )
        return cur.lastrowid


def get_project(project_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["postcodes"] = json.loads(d["postcodes"])
        return d


def get_all_projects():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["postcodes"] = json.loads(d["postcodes"])
            result.append(d)
        return result


# ── Stage status updaters ─────────────────────────────────────────────────────

def _set_status(project_id, col, status):
    with get_db() as conn:
        conn.execute(f"UPDATE projects SET {col} = ? WHERE id = ?", (status, project_id))

def update_stage1_status(project_id, status): _set_status(project_id, "stage1_status", status)
def update_stage2_status(project_id, status): _set_status(project_id, "stage2_status", status)
def update_stage3_status(project_id, status): _set_status(project_id, "stage3_status", status)
def update_stage4_status(project_id, status): _set_status(project_id, "stage4_status", status)
def update_stage5_status(project_id, status): _set_status(project_id, "stage5_status", status)
def update_stage6_status(project_id, status): _set_status(project_id, "stage6_status", status)
def update_stage7_status(project_id, status): _set_status(project_id, "stage7_status", status)
def update_stage8_status(project_id, status): _set_status(project_id, "stage8_status", status)


def update_search_areas(project_id, areas):
    """Persist the search areas (postcode districts / towns) entered at Stage 1."""
    with get_db() as conn:
        conn.execute("UPDATE projects SET postcodes = ? WHERE id = ?",
                     (json.dumps(areas), project_id))


def mark_stage_skipped(project_id, stage_num):
    col_skip = f"stage{stage_num}_skipped"
    col_status = f"stage{stage_num}_status"
    with get_db() as conn:
        conn.execute(
            f"UPDATE projects SET {col_skip} = 1, {col_status} = 'skipped' WHERE id = ?",
            (project_id,),
        )


def set_vs_sent(project_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE projects SET stage6_status = 'awaiting', vs_sent_at = ? WHERE id = ?",
            (now, project_id),
        )


# ── Unique ID counter ─────────────────────────────────────────────────────────

def claim_unique_id_block(project_id, count):
    with get_db() as conn:
        row = conn.execute("SELECT unique_id_counter FROM projects WHERE id = ?", (project_id,)).fetchone()
        start = row["unique_id_counter"]
        conn.execute(
            "UPDATE projects SET unique_id_counter = ? WHERE id = ?",
            (start + count, project_id),
        )
        return start


def set_unique_id_counter(project_id, value):
    with get_db() as conn:
        conn.execute("UPDATE projects SET unique_id_counter = ? WHERE id = ?", (value, project_id))


# ── Logs ──────────────────────────────────────────────────────────────────────

def add_log(project_id, stage, message):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO stage_logs (project_id, stage, message, ts) VALUES (?, ?, ?, ?)",
            (project_id, stage, message, ts),
        )


def get_logs(project_id, stage=None):
    with get_db() as conn:
        if stage is not None:
            rows = conn.execute(
                "SELECT ts, message FROM stage_logs WHERE project_id = ? AND stage = ? ORDER BY id",
                (project_id, stage),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT stage, ts, message FROM stage_logs WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]


# ── Apollo batches ────────────────────────────────────────────────────────────

def create_apollo_batch(project_id, batch_num, row_count, filename):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO apollo_batches (project_id, batch_num, row_count, status, filename, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (project_id, batch_num, row_count, filename, now),
        )
        return cur.lastrowid


def get_apollo_batches(project_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM apollo_batches WHERE project_id = ? ORDER BY batch_num",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_batch_status(batch_id, status):
    with get_db() as conn:
        conn.execute("UPDATE apollo_batches SET status = ? WHERE id = ?", (status, batch_id))


def delete_apollo_batches(project_id):
    """Clear existing batches so Stage 3 can be re-run."""
    with get_db() as conn:
        conn.execute("DELETE FROM apollo_batches WHERE project_id = ?", (project_id,))


# ── Stage 5 decisions ─────────────────────────────────────────────────────────

def log_s5_decision(project_id, unique_id, pass_num, label, confidence=None, reason=None):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO stage5_decisions (project_id, unique_id, pass_num, label, confidence, reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, unique_id, pass_num, label, confidence, reason, ts),
        )


def get_s5_decisions(project_id):
    """Return all decisions keyed by unique_id (latest wins if multiple)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM stage5_decisions WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    by_uid = {}
    for r in rows:
        by_uid[r["unique_id"]] = dict(r)
    return by_uid


def get_s5_review_queue(project_id):
    """
    Rows that need human review: the latest pass_num=2 record per unique_id,
    where that record has confidence<0.70 and no pass_num=3 override exists.
    Using the latest pass_num=2 means a successful Gemini retry replaces an
    earlier 'Gemini error' record correctly. The 0.70 threshold matches
    classifier.PASS2_AUTO — Gemini answers between 0.70 and 0.80 auto-apply
    rather than escalating to human.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT d2.*
            FROM stage5_decisions d2
            WHERE d2.project_id = ?
              AND d2.pass_num = 2
              AND d2.id = (
                SELECT MAX(id) FROM stage5_decisions
                WHERE project_id = d2.project_id
                  AND unique_id = d2.unique_id
                  AND pass_num = 2
              )
              AND d2.confidence < 0.70
              AND NOT EXISTS (
                SELECT 1 FROM stage5_decisions d3
                WHERE d3.project_id = d2.project_id
                  AND d3.unique_id = d2.unique_id
                  AND d3.pass_num = 3
              )
            ORDER BY d2.confidence
        """, (project_id,)).fetchall()
        return [dict(r) for r in rows]


def get_s5_pass1_scores(project_id):
    """
    Map unique_id → Pass-1 Tentative fuzzy score (0.0-1.0). Used by the review
    template to display the original similarity score next to manual decisions.
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT unique_id, confidence
            FROM stage5_decisions
            WHERE project_id = ? AND pass_num = 1 AND label = 'T'
        """, (project_id,)).fetchall()
        return {r["unique_id"]: r["confidence"] for r in rows}


# ── Stage 7 decisions ─────────────────────────────────────────────────────────

def replace_s7_decisions(project_id, decisions):
    """
    Atomically replace all Stage 7 decisions for a project in one transaction:
    the old set is deleted and the new set inserted together, so a crash can
    never leave the table half-old/half-new, and a cancelled run (which never
    calls this) leaves the previous run's audit trail fully intact.

    decisions: iterable of (unique_id, re_name, match_type, label, confidence, reason)
    """
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with get_db() as conn:
        conn.execute("DELETE FROM stage7_decisions WHERE project_id = ?", (project_id,))
        conn.executemany(
            "INSERT INTO stage7_decisions (project_id, unique_id, re_name, match_type, label, confidence, reason, pass_num, ts) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            [(project_id, uid, re_name, match_type, label, confidence, reason, ts)
             for uid, re_name, match_type, label, confidence, reason in decisions],
        )


def get_s7_decisions(project_id):
    """Return all decisions keyed by unique_id (latest wins)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM stage7_decisions WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    by_uid = {}
    for r in rows:
        by_uid[r["unique_id"]] = dict(r)
    return by_uid




def delete_project(project_id):
    """Remove project and all its associated DB rows."""
    with get_db() as conn:
        conn.execute("DELETE FROM stage_logs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM apollo_batches WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM stage5_decisions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM stage7_decisions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ── Utility ───────────────────────────────────────────────────────────────────

def project_dir(project_id, region_code):
    base = os.path.join(os.path.dirname(__file__), "projects")
    return os.path.join(base, f"{project_id}_{region_code}")
