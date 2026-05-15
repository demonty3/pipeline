"""
Text-cleanup helpers shared across stages.

Right now this is just `clean_company_name`, used by Stage 3 (Apollo magazine)
to normalise UK Companies House company names into the form Apollo expects.

Companies House returns names in ALL CAPS with a company-type suffix (LTD,
LIMITED, PLC, LLP, ...), often with a trailing country tag in brackets ("(UK)"
or trailing as " UK"). Apollo matches against a mixed-case index of trading
names — so we strip the suffix + tags and apply a sensible title-case before
sending.
"""
import re

# UK Companies House entity-type suffixes. Longest forms first so multi-word
# variants are consumed before the single-word fragments inside them.
# Bare "company" is intentionally NOT in this list — it's commonly part of
# trading names (e.g. "Smech Management Company") and only acts as a suffix
# inside "Public Limited Company", which is matched as one phrase.
_SUFFIXES = [
    "public limited company",
    "limited", "ltd",
    "plc",
    "llp", "l.l.p.",
    "lp",
    "cic", "cio", "rtm",
]

# Country / region tokens stripped from anywhere in the name (preserves the
# existing pipeline's behaviour: in the CH dataset "UK" almost always shows
# up as a geographic qualifier between brand and business unit, not as a
# brand prefix). Dotted forms ("U.K", "U.K.") are intentionally NOT stripped
# — golden behaviour keeps them, and they're rare enough to leave alone.
_COUNTRY_TOKENS = ["united kingdom", "uk"]

# Short all-consonant tokens that are abbreviations of regular words, NOT
# initialisms. These should be title-cased ("Dr", "St", "Mr") rather than
# preserved as ALL CAPS — they often show up in addresses and titles inside
# company names.
_CONSONANT_ABBREVS = {
    "dr", "st", "mr", "ms", "mrs", "sr", "jr", "fr",
    "rd", "ft", "ln", "pl", "ct",
}

_VOWELS = set("aeiou")


def clean_company_name(raw: str) -> str:
    """
    Normalise a Companies House company name for Apollo matching.

    Steps:
      1. Drop bracketed asides ("(UK)", "[Holdings]").
      2. Iteratively strip trailing entity-type suffixes and country tags,
         tidying orphan punctuation between iterations.
      3. Re-case: preserve short all-caps tokens as acronyms (PS, JB, GE,
         BBC); title-case the rest. Period-separated initials get each
         segment capitalized ("A.M.Wandsworth").

    Examples:
      "LONDON GAS INSPECTIONS LIMITED"      → "London Gas Inspections"
      "PS 13 LTD"                           → "PS 13"
      "JB MANAGEMENT (UK) LLP"              → "JB Management"
      "KIRIN PRODUCTIONS LTD."              → "Kirin Productions"
      "A.M.WANDSWORTH LIMITED"              → "A.M.Wandsworth"
      "AVON STRATEGIC COMMUNICATIONS LLP"   → "Avon Strategic Communications"
    """
    if not raw or not raw.strip():
        return ""

    # 1. Drop bracketed asides.
    s = re.sub(r"\([^)]*\)", " ", raw)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s_low = s.lower()

    # 2. Strip standalone country tokens (UK / United Kingdom) from anywhere.
    for tok in _COUNTRY_TOKENS:
        s_low = re.sub(rf"\b{re.escape(tok)}\b", " ", s_low)
    s_low = re.sub(r"\s+", " ", s_low).strip()

    # 3. Strip trailing entity-type suffix, iteratively. The `\.?` consumes a
    #    single trailing period attached to the suffix ("LTD." → "ltd"), so
    #    we don't have to rstrip periods unconditionally — that would also
    #    kill "Inc." and "Co." when they're the real end of a name.
    #    Only ORPHAN punctuation (whitespace-before) is rstripped.
    changed = True
    while changed:
        before = s_low
        s_low = re.sub(r"\s+[.,;:\-]+$", "", s_low).rstrip()
        for phrase in _SUFFIXES:
            s_low = re.sub(
                rf"(?:(?<=\s)|^){re.escape(phrase)}\.?\s*$", "", s_low
            ).rstrip()
        changed = s_low != before
    s_low = re.sub(r"\s+", " ", s_low).strip()
    s_low = re.sub(r"\s+[.,;:\-]+$", "", s_low).strip()

    # 3. Re-case each token.
    out = []
    for token in s_low.split():
        if _looks_like_acronym(token):
            out.append(token.upper())
        else:
            out.append(_smart_capitalize(token))
    return " ".join(out)


def _looks_like_acronym(token: str) -> bool:
    """
    Decide whether a token should keep ALL CAPS.

    Heuristic: short tokens (≤3 letters) with no vowels are almost always
    initialisms (PS, JB, BBC, RTM, SD, TF). Short tokens with vowels are
    usually short English words rendered in caps by CH's uppercase output
    (GE → Ge, AMP → Amp, GAS → Gas, NO → No) — title-case them.

    Common consonant-only abbreviations (Dr, St, Mr) are excluded via
    a small stoplist so addresses don't end up shouting.
    """
    bare = re.sub(r"[^a-z]", "", token)
    if not bare or len(bare) > 3:
        return False
    if bare in _CONSONANT_ABBREVS:
        return False
    return not (_VOWELS & set(bare))


def _smart_capitalize(token: str) -> str:
    """
    Title-case a token, with one wrinkle: a *leading run* of single-letter
    period-separated segments is treated as initials and each segment is
    capitalised. So "a.m.wandsworth" → "A.M.Wandsworth" but "billion-air.org"
    → "Billion-air.org" (no leading single-letter segment) and "co.creative"
    → "Co.creative" (leading segment is 2 letters, not 1).
    """
    m = re.match(r"^((?:[a-z]\.)+)([a-z].*)?$", token)
    if m:
        initials = m.group(1).upper()
        rest = m.group(2) or ""
        return initials + (rest.capitalize() if rest else "")
    return token.capitalize()
