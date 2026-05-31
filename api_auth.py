"""
api_auth.py
-----------
Preferred authentication path: obtain the Bearer token by POSTing credentials
directly to the CRM's login API, avoiding browser automation entirely.

This is only active when AUTH_LOGIN_URL is configured. The exact endpoint,
body field names, and the JSON path to the token are environment-driven so
the real values (discovered via DevTools) can be plugged in without code
changes. If the endpoint is not configured or the call fails, callers fall
back to the Playwright flow in auth.py.
"""

import re
from typing import Optional

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

import config
from logger import get_logger

log = get_logger()


class APIAuthError(Exception):
    """Raised when direct API authentication fails."""


class APIAuthNotConfigured(APIAuthError):
    """Raised when no direct-auth endpoint is configured."""


def is_configured() -> bool:
    return bool(config.AUTH_LOGIN_URL)


def _looks_like_jwt(value: str) -> bool:
    return bool(
        isinstance(value, str)
        and re.fullmatch(
            r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", value
        )
    )


def _dig(data, path: str) -> Optional[str]:
    """Follow a dot-path (e.g. 'data.access_token') through nested dicts."""
    cur = data
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur if isinstance(cur, str) else None


def _post_json(url: str, body: dict, bearer: Optional[str] = None) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": config.CRM_BASE_URL,
        "Referer": f"{config.CRM_BASE_URL}/",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise APIAuthError(f"Network error calling {url}: {exc}") from exc

    if resp.status_code in (401, 403):
        # Bad credentials are not retryable and not a "fall back" situation.
        raise APIAuthNotConfigured(
            f"Endpoint rejected credentials ({resp.status_code}) at {url}"
        )
    if not resp.ok:
        raise APIAuthError(f"{url} returned {resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise APIAuthError(f"Response from {url} was not JSON") from exc


@retry(
    reraise=True,
    stop=stop_after_attempt(config.RETRY_ATTEMPTS),
    wait=wait_exponential(min=config.RETRY_MIN_WAIT, max=config.RETRY_MAX_WAIT),
    retry=retry_if_exception_type(APIAuthError),
)
def get_access_token() -> str:
    """
    Two-step Kenko/Bookee authentication:
      1. POST credentials to the login endpoint -> identity_token.
      2. POST the client_id to the switch endpoint (with the identity token)
         -> the real access_token used for the export API.

    Raises APIAuthNotConfigured if no endpoint is set so the caller can fall
    back to Playwright.
    """
    if not is_configured():
        raise APIAuthNotConfigured("AUTH_LOGIN_URL is not set")

    # --- Step 1: login -----------------------------------------------------
    login_body = {
        config.AUTH_USERNAME_FIELD: config.CRM_USERNAME,
        config.AUTH_PASSWORD_FIELD: config.CRM_PASSWORD,
    }
    login_body.update(config.get_auth_extra_body())

    log.info("Attempting direct API authentication (step 1: login)")
    login_data = _post_json(config.AUTH_LOGIN_URL, login_body)

    identity_token = _dig(login_data, config.AUTH_TOKEN_JSON_PATH)
    if not identity_token:
        raise APIAuthError(
            f"Identity token not found at JSON path '{config.AUTH_TOKEN_JSON_PATH}'"
        )

    # --- Step 2: switch to the selected client (business/location) ---------
    # If no switch endpoint or client_id is configured, assume the identity
    # token is sufficient and return it directly.
    if not config.AUTH_SWITCH_URL or not config.AUTH_CLIENT_ID:
        log.info("No client switch configured; using login token directly")
        log.info("Login successful (direct API)")
        log.info("Bearer token extracted")
        return identity_token

    log.info("Direct API authentication (step 2: switch client)")
    switch_body = {config.AUTH_CLIENT_ID_FIELD: config.AUTH_CLIENT_ID}
    switch_data = _post_json(
        config.AUTH_SWITCH_URL, switch_body, bearer=identity_token
    )

    access_token = _dig(switch_data, config.AUTH_SWITCH_TOKEN_JSON_PATH)
    if not access_token:
        raise APIAuthError(
            f"Access token not found at JSON path "
            f"'{config.AUTH_SWITCH_TOKEN_JSON_PATH}'"
        )

    log.info("Login successful (direct API, client selected)")
    log.info("Bearer token extracted")
    return access_token
