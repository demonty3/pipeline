"""
Pre-flight probe for the Gemini API.

Returns a short status string the caller can use to route the run:
    "ok"            — Gemini answered, we can proceed with Pass 2
    "no_key"        — GEMINI_API_KEY is empty / missing
    "auth"          — key was rejected (401/403/PermissionDenied)
    "rate_limited"  — quota exhausted / 429
    "network"       — DNS / TLS / timeout / other transport error
    "unknown"       — Gemini raised, but the cause didn't fit the buckets above

Plus a `details` string with the raw exception message for the operator.
"""
import google.generativeai as genai

MODEL = "gemini-2.5-flash"
PROBE_PROMPT = "Return the literal text: ok"


def ping(api_key):
    if not api_key:
        return "no_key", "GEMINI_API_KEY is not set"

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(MODEL)
        # request_options keeps the probe from hanging if the API is slow.
        resp = model.generate_content(PROBE_PROMPT, request_options={"timeout": 5})
        _ = resp.text  # force evaluation; some failures only surface on access
        return "ok", "Gemini responded"
    except Exception as exc:
        return _classify(exc), str(exc)


def _classify(exc):
    msg = str(exc).lower()
    name = type(exc).__name__.lower()

    if "429" in msg or "quota" in msg or "rate" in msg or "resourceexhausted" in name:
        return "rate_limited"
    if "401" in msg or "403" in msg or "permission" in msg or "unauthenticated" in name or "api key" in msg:
        return "auth"
    if "timeout" in msg or "timed out" in msg or "deadline" in msg or "unavailable" in msg \
            or "connection" in msg or "dns" in msg or "network" in msg:
        return "network"
    return "unknown"


HUMAN_MESSAGES = {
    "no_key":       "Gemini API key is not configured.",
    "auth":         "Gemini rejected the API key (auth error).",
    "rate_limited": "Gemini is rate-limited or out of quota.",
    "network":      "Could not reach Gemini (network/timeout).",
    "unknown":      "Gemini returned an unexpected error.",
    "ok":           "Gemini is reachable.",
}


def human(status):
    return HUMAN_MESSAGES.get(status, status)
