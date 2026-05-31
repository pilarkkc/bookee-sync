"""
token_manager.py
----------------
In-memory token cache. Avoids unnecessary work: the login only runs when no
token exists yet or when a caller forces a refresh (e.g. after a 401).

Auth strategy, in order of preference:
  1. Direct API authentication (api_auth) when AUTH_LOGIN_URL is configured.
  2. Playwright browser login (auth) as a fallback.
"""

from typing import Optional

import api_auth
import auth
from logger import get_logger

log = get_logger()


def _obtain_token() -> str:
    """Prefer direct API auth; fall back to Playwright when unavailable."""
    if api_auth.is_configured():
        try:
            return api_auth.get_access_token()
        except api_auth.APIAuthNotConfigured as exc:
            log.info(f"Direct API auth unavailable ({exc}); using browser login")
        except api_auth.APIAuthError as exc:
            log.info(f"Direct API auth failed ({exc}); falling back to browser login")
    return auth.get_access_token()


class TokenManager:
    def __init__(self) -> None:
        self._token: Optional[str] = None

    def get_token(self, force_refresh: bool = False) -> str:
        """Return a cached token, authenticating only when required."""
        if self._token and not force_refresh:
            return self._token
        if force_refresh:
            log.info("Token refresh required - re-authenticating")
        self._token = _obtain_token()
        return self._token

    def invalidate(self) -> None:
        self._token = None


# Module-level singleton so the in-memory token persists across calls.
token_manager = TokenManager()
