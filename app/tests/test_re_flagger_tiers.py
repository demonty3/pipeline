"""
Tier-logic test for `stages.re_flagger` (Stage 7).

RE matching is deterministic formulas only — no LLM ever touches RE donor
data (Charles, 2026-06-10). The normative tier rules live in
cchq-orchestrator/references/schema_contract.md; the cases below encode them,
including the regressions: tier outranks raw name score when picking the best
RE candidate, and a VoteSource `EmailAddress` counts as email evidence
alongside `Apollo Email`. The 2026-06-12 tightening (Charles): postcode
corroboration is the FULL postcode, and a person match without email evidence
requires the officer's surname to appear in the RE name.

Builds a synthetic master + RE export covering each tier and asserts both the
summary counts and the columns written to the re_flagged master.

Run from project root:
    python app/tests/test_re_flagger_tiers.py
"""
import os
import sys
import tempfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
sys.path.insert(0, APP_DIR)

from stages.re_flagger import (  # noqa: E402
    run_re_flagging, apply_re_decisions_and_save, _tier, _full_pc,
    _surname_agrees,
)

MASTER_COLS = [
    "Unique ID", "Surname", "First Name", "Company Name", "Officer name",
    "Officer address post code", "Company address post code",
    "Officer address locality", "Company address locality",
    "Apollo Email", "EmailAddress",
]


class FakeDB:
    """Captures decisions in memory, mimicking database.py's Stage 7 API."""

    def __init__(self):
        self.decisions = {}

    def replace_s7_decisions(self, project_id, decisions):
        self.decisions = {
            uid: {"label": label, "re_name": re_name, "match_type": match_type,
                  "confidence": confidence, "reason": reason}
            for uid, re_name, match_type, label, confidence, reason in decisions
        }

    def get_s7_decisions(self, project_id):
        return self.decisions


def _master_row(uid, surname, first, postcode="", locality="", email="", vs_email=""):
    return {
        "Unique ID": uid, "Surname": surname, "First Name": first,
        "Company Name": "", "Officer name": f"{surname.upper()}, {first}",
        "Officer address post code": postcode, "Company address post code": "",
        "Officer address locality": locality, "Company address locality": "",
        "Apollo Email": email, "EmailAddress": vs_email,
    }


def test_unit_tier_formula():
    # (name_score, email, postcode, town, surname_ok) → expected tier
    cases = [
        ((100, True, False, False, True), "match"),      # email + exact name
        ((80, True, False, False, True), "match"),       # email + plausible name
        ((40, True, False, False, True), "probable"),    # email but weak name (shared mailbox guard)
        ((40, True, False, False, False), "probable"),   # email exempt from the surname rule
        ((95, False, True, False, True), "probable"),    # strong name + postcode
        ((95, False, False, True, True), "probable"),    # strong name + town
        ((100, False, False, False, True), "potential"), # exact name ALONE stays potential
        ((80, False, True, True, True), "potential"),    # medium name; geo only elevates strong names
        ((95, False, True, True, False), None),          # no email + surname disagrees → no flag
        ((70, False, False, False, True), None),         # below the flag floor
    ]
    for args, want in cases:
        got = _tier(*args)
        assert got == want, f"_tier{args}: want {want}, got {got}"
    print(f"unit: {len(cases)} tier-formula cases OK")


def test_unit_full_pc():
    cases = [
        ("CM1 2AB", "CM12AB"),   # full postcode, space stripped
        ("cm12ab", "CM12AB"),    # case normalised
        ("LE12", "LE12"),        # outward-only passes through (never equals a full code)
        ("", ""),
    ]
    for raw, want in cases:
        got = _full_pc(raw)
        assert got == want, f"_full_pc({raw!r}): want {want!r}, got {got!r}"
    print(f"unit: {len(cases)} full-postcode cases OK")


def test_unit_surname_agrees():
    cases = [
        (("Bowden", "stephen brown"), False),          # same first name ≠ same person
        (("Kerner", "john kerr"), False),              # kerner/kerr ratio 80 < 90
        (("Smith", "john smith"), True),               # exact token
        (("RUIZ SANTOS", "diana ruiz santos"), True),  # multi-token surname
        (("Lee", "james lee"), True),                  # short surname, exact token
        (("O'Brien", "mary obrien"), True),            # punctuation drift via ≥90 ratio
    ]
    for (surname, re_norm), want in cases:
        got = _surname_agrees(surname, re_norm.split())
        assert got == want, f"_surname_agrees({surname!r}, {re_norm!r}): want {want}, got {got}"
    print(f"unit: {len(cases)} surname-agreement cases OK")


def test_end_to_end_tiers():
    master = pd.DataFrame([
        # → Match: same name AND same email
        _master_row("#TT1-0001", "Pemberton", "Alice", email="alice@pemberton.co.uk"),
        # → Probable: same email, weak name (married-name change found via email index)
        _master_row("#TT1-0002", "Okafor-Hughes", "Chinwe", email="c.hughes@mail.com"),
        # → Potential: strong name + same OUTWARD postcode only — since
        #   2026-06-12 the corroboration is the full postcode, so this no
        #   longer earns Probable
        _master_row("#TT1-0003", "Szymanski", "Bartholomew", postcode="LE2 4QT"),
        # → Potential: exact name, no corroborating factor
        _master_row("#TT1-0004", "Smith", "John"),
        # → no flag: nothing close in RE
        _master_row("#TT1-0005", "Zhukovsky", "Yelena"),
        # → Probable: the email-matched donor must beat a name-only decoy —
        #   tier outranks raw name score in the best-candidate pick
        _master_row("#TT1-0006", "Watson", "Emily", email="e.watson@corp.com"),
        # → Potential: RE side carries an outward-only postcode — can never
        #   equal a full postcode, so no corroboration
        _master_row("#TT1-0007", "Quattrocchi", "Lorenzo", postcode="LE12 8TT"),
        # → Match: email evidence comes from the VoteSource EmailAddress column
        _master_row("#TT1-0008", "Fitzwilliam", "Harriet", vs_email="h.fitz@mail.com"),
        # → no flag: shared first name + similar-looking surname token-sorts
        #   to ~89, but the surname disagrees and there's no email evidence
        _master_row("#TT1-0009", "Bowden", "Stephen"),
        # → Probable: strong name + same FULL postcode (household-level)
        _master_row("#TT1-0010", "Beaumont", "Olivia", postcode="LE2 4QT"),
    ], columns=MASTER_COLS)

    re_export = pd.DataFrame([
        {"Name": "Mrs Alice Pemberton", "Postcode": "", "City": "", "Email address": "alice@pemberton.co.uk"},
        {"Name": "Chinwe Adaeze", "Postcode": "", "City": "", "Email address": "c.hughes@mail.com"},
        {"Name": "Bartholomew Szymanski", "Postcode": "LE2 9ZZ", "City": "", "Email address": ""},
        {"Name": "John Smith", "Postcode": "", "City": "", "Email address": ""},
        {"Name": "Quentin Farquharson", "Postcode": "", "City": "", "Email address": ""},
        # decoy: exact name, zero factors (tier Potential, raw rank 100)
        {"Name": "Emily Watson", "Postcode": "", "City": "", "Email address": ""},
        # the actual donor: weak name, exact email (tier Probable, raw rank ~75)
        {"Name": "Mrs E Hughes", "Postcode": "", "City": "", "Email address": "e.watson@corp.com"},
        {"Name": "Lorenzo Quattrocchi", "Postcode": "LE12", "City": "", "Email address": ""},
        {"Name": "Harriet Fitzwilliam", "Postcode": "", "City": "", "Email address": "h.fitz@mail.com"},
        {"Name": "Stephen Brown", "Postcode": "", "City": "", "Email address": ""},
        {"Name": "Olivia Beaumont", "Postcode": "LE2 4QT", "City": "", "Email address": ""},
    ])

    want = {
        "#TT1-0001": ("Y", "Match"),
        "#TT1-0002": ("Y", "Probable"),
        "#TT1-0003": ("Y", "Potential"),   # outward-only agreement demoted 2026-06-12
        "#TT1-0004": ("Y", "Potential"),
        "#TT1-0005": ("N", ""),
        "#TT1-0006": ("Y", "Probable"),
        "#TT1-0007": ("Y", "Potential"),   # outward-only agreement demoted 2026-06-12
        "#TT1-0008": ("Y", "Match"),
        "#TT1-0009": ("N", ""),            # surname disagrees, no email → no flag
        "#TT1-0010": ("Y", "Probable"),    # full-postcode corroboration
    }

    with tempfile.TemporaryDirectory() as tmp:
        master.to_csv(os.path.join(tmp, "master_TT1_classified.csv"), index=False)
        re_path = os.path.join(tmp, "re_export_TT1.csv")
        re_export.to_csv(re_path, index=False)

        db = FakeDB()
        summary = run_re_flagging(1, tmp, "TT1", re_path, progress_cb=print, db=db)
        apply_re_decisions_and_save(1, tmp, "TT1", db)

        out = pd.read_csv(os.path.join(tmp, "master_TT1_re_flagged.csv"),
                          dtype=str, keep_default_na=False)
        for _, row in out.iterrows():
            uid = row["Unique ID"]
            got = (row["RE Match?"], row["Potential"])
            assert got == want[uid], f"{uid}: want {want[uid]}, got {got} ({row['Match?']!r})"
            print(f"  {uid}: RE Match?={got[0]:1} Potential={got[1]:9} factors={row['Match?']!r}")

        # the email-found donor must be the recorded RE hit, not the decoy
        assert db.decisions["#TT1-0006"]["re_name"] == "Mrs E Hughes", db.decisions["#TT1-0006"]

    expected = {"match": 2, "probable": 3, "potential": 3, "no_flag": 2, "cancelled": False}
    assert summary == expected, summary
    print(f"end-to-end: summary {summary} OK")


def test_cancelled_run_keeps_old_decisions():
    # A cancelled run must leave the previous run's decisions untouched.
    import threading
    master = pd.DataFrame([_master_row("#TT1-0001", "Pemberton", "Alice")],
                          columns=MASTER_COLS)
    re_export = pd.DataFrame([{"Name": "Alice Pemberton"}])

    with tempfile.TemporaryDirectory() as tmp:
        master.to_csv(os.path.join(tmp, "master_TT1_classified.csv"), index=False)
        re_path = os.path.join(tmp, "re_export_TT1.csv")
        re_export.to_csv(re_path, index=False)

        db = FakeDB()
        db.decisions = {"#OLD-0001": {"label": "match", "re_name": "Old Donor",
                                      "match_type": "person", "confidence": 0.99,
                                      "reason": "previous run"}}
        ev = threading.Event()
        ev.set()  # cancelled before the first row
        summary = run_re_flagging(1, tmp, "TT1", re_path, db=db, cancel_event=ev)

        assert summary["cancelled"] is True, summary
        assert "#OLD-0001" in db.decisions, "cancel wiped the previous audit trail"
    print("cancel: previous decisions preserved OK")


def test_no_llm_imports():
    # The hard constraint, encoded: the module must not import any LLM client.
    import stages.re_flagger as rf
    src = open(rf.__file__).read()
    for banned in ("google.generativeai", "genai", "openai", "anthropic"):
        assert banned not in src, f"LLM reference {banned!r} found in re_flagger.py"
    print("no-LLM check: OK")


if __name__ == "__main__":
    test_unit_tier_formula()
    test_unit_full_pc()
    test_unit_surname_agrees()
    test_end_to_end_tiers()
    test_cancelled_run_keeps_old_decisions()
    test_no_llm_imports()
    print("ALL OK")
