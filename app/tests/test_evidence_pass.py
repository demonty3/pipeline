"""
Tests for the Stage 5 deterministic evidence pass (replaced Gemini, 2026-06-11).

Run:  cd app && python3 tests/test_evidence_pass.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from stages.classifier import _evidence_pass2, _email_corroborates, _canonical_first_names


class FakeDB:
    """Collects log_s5_decision calls so we can assert on them."""
    def __init__(self):
        self.decisions = []

    def log_s5_decision(self, project_id, uid, pass_num, label, confidence=None, reason=None):
        self.decisions.append({"uid": uid, "pass": pass_num, "label": label,
                               "confidence": confidence, "reason": reason})


def _run(rows):
    db = FakeDB()
    with tempfile.TemporaryDirectory() as d:
        log_path = os.path.join(d, "log.csv")
        y, review = _evidence_pass2(1, rows, log_path, db, lambda m: None)
    return y, review, {d["uid"]: d for d in db.decisions}


def _row(uid, officer, apollo, surname="", first="", email=""):
    return {"unique_id": uid, "officer_name": officer, "apollo_name": apollo,
            "company_name": "X LTD", "surname": surname, "first_name": first,
            "apollo_email": email}


def test_nickname_upgrades_to_y():
    """Tom Carson vs THOMAS CARSON — Pass-1 fuzzy missed it, nicknames catch it."""
    y, review, dec = _run([_row("#T-1", "CARSON, Thomas", "Tom Carson",
                                surname="Carson", first="Thomas")])
    assert y == 1 and review == 0
    assert dec["#T-1"]["label"] == "Y"
    assert "Nickname" in dec["#T-1"]["reason"]


def test_email_corroboration_upgrades_to_y():
    """Officer David Bennett + david.bennett@… — same person, different display name."""
    y, review, dec = _run([_row("#T-2", "BENNETT, David", "D Bennett-Smythe",
                                surname="Bennett", first="David",
                                email="david.bennett@changeharbour.com")])
    assert y == 1
    assert dec["#T-2"]["label"] == "Y"
    assert "Email" in dec["#T-2"]["reason"]


def test_no_evidence_stays_t():
    y, review, dec = _run([_row("#T-3", "KENNEDY, Nigel", "Leon Chadwick",
                                surname="Kennedy", first="Nigel",
                                email="techmentor@gmail.com")])
    assert y == 0 and review == 1
    assert dec["#T-3"]["label"] == "T"


def test_short_surname_email_guard():
    """Surname 'Li' must not match alice@… (substring false positive)."""
    assert not _email_corroborates("Li", "Wei", "alice@example.com")
    # but the first-initial+surname form is allowed at any length: wli@…
    assert _email_corroborates("Li", "Wei", "wli@example.com")


def test_canonicalisation_is_symmetric():
    assert _canonical_first_names("Andy Low") == _canonical_first_names("ANDREW LOW")
    # unknown names pass through untouched
    assert _canonical_first_names("Folashade Ahmed") == "folashade ahmed"


def test_never_downgrades_to_n():
    """The evidence pass must never emit N — absence of evidence is not mismatch."""
    rows = [_row(f"#T-{i}", "A B", "C D", surname="B", first="A") for i in range(5)]
    _, _, dec = _run(rows)
    assert all(d["label"] in ("Y", "T") for d in dec.values())


if __name__ == "__main__":
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
