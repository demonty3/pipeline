"""
Golden-file regression test for `stages.text_cleanup.clean_company_name`.

Runs the cleaner over `results_SW3.csv`'s Company Name column and compares
to `results_SW3_for_enrichment.csv`. The SW3 enrichment file was produced
by the older cleaner that had four known bug-classes; the new cleaner
intentionally diverges from the golden file in those cases. This test
buckets each diff into a category and fails only on diffs that don't
match a known-improvement pattern.

Categories tracked:
  - acronym_preserved:    no-vowel ≤3-letter token kept as ALL CAPS
  - llp_stripped:         LLP suffix now removed (golden left "Llp")
  - cic_stripped:         CIC suffix now removed (golden left "Cic")
  - orphan_period:        trailing "." after "LTD." now cleaned up
  - dotted_initials:      "A.M.X" segments each capitalised
  - trailing_period_kept: letter-attached "Inc."/"Co."/"B.V." preserved
  - other:                genuinely unexpected — investigate

Run from project root:
    python app/tests/test_clean_company_name.py
"""
import csv
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(APP_DIR)
sys.path.insert(0, APP_DIR)

from stages.text_cleanup import clean_company_name  # noqa: E402

RAW_PATH = os.path.join(PROJECT_ROOT, "results_SW3.csv")
GOLDEN_PATH = os.path.join(PROJECT_ROOT, "results_SW3_for_enrichment.csv")

_VOWELS = set("aeiou")


def _classify(raw: str, golden: str, new: str) -> str:
    """Return a category label for this diff, or 'other' if unclassifiable."""
    raw_l = raw.lower()
    golden_l = golden.lower()
    new_l = new.lower()

    # LLP stripping
    if raw_l.endswith("llp") and "llp" in golden_l and "llp" not in new_l:
        return "llp_stripped"

    # CIC stripping (Community Interest Company — real CH entity type)
    if raw_l.endswith("cic") and "cic" in golden_l and "cic" not in new_l:
        return "cic_stripped"

    # Orphan trailing period: golden ends with " ." or has " . " before more,
    # new doesn't
    if " ." in golden and " ." not in new:
        return "orphan_period"

    # Letter-attached period preserved: new keeps "Inc.", "Co.", "B.V." etc;
    # golden's blanket rstrip stripped it. Detect by: golden lacks a trailing
    # period that new has, OR golden's last token loses its trailing period.
    if new.rstrip(".") == golden.rstrip(".") + "" and new.endswith(".") and not golden.endswith("."):
        return "trailing_period_kept"

    # Dotted initials: golden has a lowercase letter immediately after a
    # period-with-uppercase ("U.k", "A.i."), new has uppercase ("U.K",
    # "A.I."). Trailing period optional so "U.k" (no terminal period) matches.
    if re.search(r"\b[A-Z]\.[a-z]\b", golden) and re.search(r"\b[A-Z]\.[A-Z]\b", new):
        return "dotted_initials"

    # Acronym preserved: any token that exists in both new and golden
    # case-insensitively, is ALL CAPS in new, ≤3 letters, no vowels.
    new_toks = new.split()
    golden_toks = golden.split()
    if len(new_toks) == len(golden_toks):
        for nt, gt in zip(new_toks, golden_toks):
            if nt.lower() != gt.lower():
                continue
            bare = re.sub(r"[^a-z]", "", nt.lower())
            if (
                bare
                and len(bare) <= 3
                and nt == nt.upper()
                and not (_VOWELS & set(bare))
            ):
                return "acronym_preserved"

    return "other"


def main():
    with open(RAW_PATH, newline="") as f1, open(GOLDEN_PATH, newline="") as f2:
        raw_rows = list(csv.DictReader(f1))
        golden_rows = list(csv.DictReader(f2))

    assert len(raw_rows) == len(golden_rows), "Row count mismatch between input files"
    print(f"Loaded {len(raw_rows):,} row pairs")

    bucket_counts = Counter()
    bucket_examples = {}  # category → set of raw names (deduped)
    matches = 0

    for raw_row, golden_row in zip(raw_rows, golden_rows):
        raw = raw_row["Company Name"]
        golden = golden_row["Company Name"]
        new = clean_company_name(raw)

        if new == golden:
            matches += 1
            continue

        category = _classify(raw, golden, new)
        bucket_counts[category] += 1
        bucket_examples.setdefault(category, []).append((raw, golden, new))

    print(f"\n  matches: {matches:,}\n")

    for category in ("acronym_preserved", "llp_stripped", "cic_stripped",
                     "orphan_period", "dotted_initials",
                     "trailing_period_kept", "other"):
        count = bucket_counts.get(category, 0)
        unique_raws = len({r for r, _, _ in bucket_examples.get(category, [])})
        marker = "  " if category != "other" else "!!"
        print(f"{marker} {category:22} {count:>5} diffs ({unique_raws} unique raw names)")

    other_examples = bucket_examples.get("other", [])
    if other_examples:
        # Dedup by raw name for readability
        seen = set()
        unique = []
        for trio in other_examples:
            if trio[0] not in seen:
                seen.add(trio[0])
                unique.append(trio)
        print(f"\nFirst 20 unique 'other' diffs:")
        for raw, golden, new in unique[:20]:
            print(f"  raw:     {raw}")
            print(f"  golden:  {golden}")
            print(f"  cleaner: {new}")
            print()
        sys.exit(1)

    print("\nOK — all diffs are intentional improvements over the SW3 baseline.")


if __name__ == "__main__":
    main()
