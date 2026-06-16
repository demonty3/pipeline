"""
Regression tests for the Essex Apollo mis-join fix (2026-06-11).

Fix A (apollo_ingest): a Unique ID that points at a DIFFERENT person must NOT
        carry enrichment onto that row — the join must verify identity first.
Fix B (merge):         re-merging must REUSE each person's existing Unique ID,
        never renumber by row order.

Run:  cd app && python -m pytest test_misjoin_fixes.py -v
   or: cd app && python test_misjoin_fixes.py   (no pytest needed)
"""
import os
import csv
import tempfile

import pandas as pd

from stages.apollo_ingest import ingest_batch, _identity_matches
from stages.merge import run_merge, _load_uid_map

RC = "T1"

# The 26 Companies House columns run_merge expects (from merge.py).
CH_COLS = [
    "Surname", "First Name", "Middle Names", "Officer name",
    "Officer occupation", "Officer role", "Officer nationality",
    "Officer date of birth", "Officer address line one",
    "Officer address locality", "Officer address country",
    "Officer address post code", "Officer country of residence",
    "Officer appointment date", "Appointment", "Officer resignation date",
    "Company Name", "Company Number", "Company Status", "Company Type",
    "Company date of creation", "Company address line one",
    "Company address locality", "Company address country",
    "Company address post code", "Company SIC codes",
]


def _ch_row(surname, first, dob, company, company_number):
    r = {c: "" for c in CH_COLS}
    r.update({"Surname": surname, "First Name": first, "Officer date of birth": dob,
              "Officer name": f"{surname.upper()}, {first}", "Company Name": company,
              "Company Number": company_number, "Company SIC codes": "62012"})
    return r


def _write_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ──────────────────────────────────────────────────────────────────────────
# Fix A — identity-verified ingest
# ──────────────────────────────────────────────────────────────────────────

_MASTER_PEOPLE = [
    ("#T1-0001", "Smith", "John", "Jan-1970", "ACME LIMITED", "111"),
    ("#T1-0002", "Jones", "Mary", "Feb-1980", "BETA LIMITED", "222"),
    ("#T1-0003", "Brown", "Bob", "Mar-1975", "GAMMA LIMITED", "333"),
    ("#T1-0004", "Green", "Gail", "Apr-1985", "DELTA LIMITED", "444"),
]


def _make_master(d):
    """Raw master with four people (#T1-0001 Smith … #T1-0004 Green)."""
    cols = ["Unique ID", "Surname", "First Name", "Officer date of birth",
            "Officer name", "Company Name", "Company Number"]
    rows = [{"Unique ID": uid, "Surname": s, "First Name": f,
             "Officer date of birth": dob, "Officer name": f"{s.upper()}, {f}",
             "Company Name": co, "Company Number": cn}
            for uid, s, f, dob, co, cn in _MASTER_PEOPLE]
    _write_csv(os.path.join(d, f"master_{RC}_raw.csv"), rows, cols)


def _return_row(uid, surname, first, company, email):
    """An Apollo return row: preserved identity cols + Apollo's Email enrichment."""
    return {"Unique ID": uid, "Surname": surname, "First Name": first,
            "Company Name": company, "Email": email}


def _ingest(d, return_rows):
    path = os.path.join(d, "apollo_return.csv")
    _write_csv(path, return_rows, ["Unique ID", "Surname", "First Name", "Company Name", "Email"])
    return ingest_batch(d, RC, path, batch_id=1)


def _enriched(d):
    return pd.read_csv(os.path.join(d, f"master_{RC}_enriched.csv"),
                       dtype=str, keep_default_na=False)


def test_wrong_person_uid_is_not_applied_and_name_recovers():
    """One mis-keyed row among several correct ones: a return stamped #T1-0001
    but carrying GREEN's data must NOT land on Smith (0001); the name fallback
    should place it on Green (0004). Smith keeps its own correctly-keyed data."""
    with tempfile.TemporaryDirectory() as d:
        _make_master(d)
        rows = [
            _return_row("#T1-0001", "Smith", "John", "ACME LIMITED", "john@acme.com"),   # correct
            _return_row("#T1-0002", "Jones", "Mary", "BETA LIMITED", "mary@beta.com"),   # correct
            _return_row("#T1-0003", "Brown", "Bob", "GAMMA LIMITED", "bob@gamma.com"),   # correct
            _return_row("#T1-0001", "Green", "Gail", "DELTA LIMITED", "gail@delta.com"), # mis-keyed
        ]
        _ingest(d, rows)  # 1/4 conflict = 25% < gate threshold → per-row guard handles it
        e = _enriched(d)
        smith = e[e["Unique ID"] == "#T1-0001"].iloc[0]
        green = e[e["Unique ID"] == "#T1-0004"].iloc[0]
        assert smith["Apollo Email"] == "john@acme.com", "Smith must keep its own data, not Green's"
        assert green["Apollo Email"] == "gail@delta.com", "name fallback should recover onto Green"


def test_correct_uid_is_applied():
    with tempfile.TemporaryDirectory() as d:
        _make_master(d)
        _ingest(d, [_return_row("#T1-0001", "Smith", "John", "ACME LIMITED", "john@acme.com")])
        e = _enriched(d)
        smith = e[e["Unique ID"] == "#T1-0001"].iloc[0]
        assert smith["Apollo Email"] == "john@acme.com"


def test_blank_first_name_passes_on_surname_only():
    """Return files often leave First Name blank — Surname agreement is enough."""
    with tempfile.TemporaryDirectory() as d:
        _make_master(d)
        _ingest(d, [_return_row("#T1-0001", "Smith", "", "ACME LIMITED", "j@acme.com")])
        e = _enriched(d)
        assert e[e["Unique ID"] == "#T1-0001"].iloc[0]["Apollo Email"] == "j@acme.com"


def test_preflight_gate_refuses_wholesale_renumber():
    """A file where every UID names the wrong person should be refused outright."""
    with tempfile.TemporaryDirectory() as d:
        _make_master(d)
        rows = [_return_row("#T1-0001", "Jones", "Mary", "BETA LIMITED", "a@b.com"),
                _return_row("#T1-0002", "Smith", "John", "ACME LIMITED", "c@d.com")]
        path = os.path.join(d, "bad.csv")
        _write_csv(path, rows, ["Unique ID", "Surname", "First Name", "Company Name", "Email"])
        try:
            ingest_batch(d, RC, path, batch_id=2)
        except ValueError as e:
            assert "RENUMBERED" in str(e)
        else:
            raise AssertionError("expected ingest to refuse the renumbered file")


# ──────────────────────────────────────────────────────────────────────────
# Fix B — stable Unique IDs across re-merges
# ──────────────────────────────────────────────────────────────────────────

def _seed_postcodes(d, rows, fname="results_T1A.csv"):
    pdir = os.path.join(d, "postcodes")
    os.makedirs(pdir, exist_ok=True)
    _write_csv(os.path.join(pdir, fname), rows, CH_COLS)


def test_remerge_reuses_unique_ids():
    with tempfile.TemporaryDirectory() as d:
        a = _ch_row("Smith", "John", "Jan-1970", "ACME LIMITED", "111")
        b = _ch_row("Jones", "Mary", "Feb-1980", "BETA LIMITED", "222")
        _seed_postcodes(d, [a, b])
        run_merge(d, RC, RC, 0)
        m1 = pd.read_csv(os.path.join(d, f"master_{RC}_raw.csv"), dtype=str, keep_default_na=False)
        uid_smith = m1[m1["Surname"] == "Smith"].iloc[0]["Unique ID"]
        uid_jones = m1[m1["Surname"] == "Jones"].iloc[0]["Unique ID"]

        # Simulate a re-fetch that ADDS a person and reorders (new file sorts first).
        c = _ch_row("Adams", "Zoe", "Mar-1990", "GAMMA LIMITED", "333")
        _seed_postcodes(d, [c, a, b], fname="results_T1A.csv")  # overwrite, new row first
        _, _ = run_merge(d, RC, RC, 0)
        m2 = pd.read_csv(os.path.join(d, f"master_{RC}_raw.csv"), dtype=str, keep_default_na=False)

        assert m2[m2["Surname"] == "Smith"].iloc[0]["Unique ID"] == uid_smith, "Smith renumbered!"
        assert m2[m2["Surname"] == "Jones"].iloc[0]["Unique ID"] == uid_jones, "Jones renumbered!"
        new_uid = m2[m2["Surname"] == "Adams"].iloc[0]["Unique ID"]
        assert new_uid not in (uid_smith, uid_jones), "new person reused an existing ID"

        # Every Unique ID is still unique.
        assert m2["Unique ID"].is_unique
        # The persisted map exists.
        uid_map, counter = _load_uid_map(d, RC)
        assert len(uid_map) == 3


def test_remerge_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        a = _ch_row("Smith", "John", "Jan-1970", "ACME LIMITED", "111")
        b = _ch_row("Jones", "Mary", "Feb-1980", "BETA LIMITED", "222")
        _seed_postcodes(d, [a, b])
        run_merge(d, RC, RC, 0)
        m1 = pd.read_csv(os.path.join(d, f"master_{RC}_raw.csv"), dtype=str, keep_default_na=False)
        run_merge(d, RC, RC, 0)
        m2 = pd.read_csv(os.path.join(d, f"master_{RC}_raw.csv"), dtype=str, keep_default_na=False)
        assert list(m1["Unique ID"]) == list(m2["Unique ID"]), "re-merge changed IDs"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
