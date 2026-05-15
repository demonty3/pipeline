"""
Apollo credit-balance lookup (best-effort).

What it does, in plain English:
  Ask Apollo's API how many enrichment credits are left on the team's
  account, so Stage 3's magazine can size its batches without
  overspending. Designed to fail soft: if there's no key, no network,
  or Apollo returns an unexpected shape, returns None and lets the
  caller fall back to operator-supplied input.

Why this is a "best-effort":
  Apollo's API has revised its account / credits endpoint a few times
  over the years and the public docs don't pin a stable "credits
  remaining" path. The code below tries the documented health endpoint
  and walks a few likely keys in the response. If your account exposes
  credits under a different key, update _extract_credits() — the rest
  of the module won't need changes.

Setup:
  Put APOLLO_API_KEY in the project's .env file. Don't commit it.

Usage:
  from stages.apollo_credits import fetch_credits_remaining
  credits = fetch_credits_remaining()
  if credits is None:
      # ask the operator to enter manually
"""
import os
import requests
from typing import Optional

APOLLO_API_BASE = "https://api.apollo.io"
HEALTH_ENDPOINT = "/api/v1/auth/health"
DEFAULT_TIMEOUT = 5  # seconds

# Keys Apollo has historically used for credits at the top level or
# nested inside team/user/account objects.
_CREDIT_KEYS = ("credits_remaining", "credits_left", "remaining_credits",
                "credit_balance", "credits")
_PARENT_KEYS = ("team", "user", "account", "data")


def fetch_credits_remaining() -> Optional[int]:
    """
    Return the number of Apollo credits remaining for the configured team,
    or None if anything goes wrong (no key, network error, parse error,
    or Apollo returns a shape we don't recognise).
    """
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        return None

    try:
        response = requests.get(
            f"{APOLLO_API_BASE}{HEALTH_ENDPOINT}",
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "application/json",
            },
            params={"api_key": api_key},
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return None

    return _extract_credits(data)


def _extract_credits(data) -> Optional[int]:
    """Walk the response looking for a credits-remaining-like integer."""
    if not isinstance(data, dict):
        return None
    for key in _CREDIT_KEYS:
        val = data.get(key)
        if isinstance(val, int):
            return val
    for parent_key in _PARENT_KEYS:
        parent = data.get(parent_key)
        if isinstance(parent, dict):
            for key in _CREDIT_KEYS:
                val = parent.get(key)
                if isinstance(val, int):
                    return val
    return None
