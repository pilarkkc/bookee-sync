"""
auth.py
-------
Handles Playwright-based login to the Kenko/Bookee CRM and extraction of the
Bearer access token. The token is never hardcoded; it is captured at runtime
from the authenticated session (network requests + browser storage).
"""

import json
import re
from typing import Optional

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

import config
from logger import get_logger

log = get_logger()


class AuthError(Exception):
    """Raised when login or token extraction fails."""


def _looks_like_jwt(value: str) -> bool:
    # Bearer tokens here are JWTs: three base64url segments separated by dots.
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", value))


def _extract_from_authorization(header_value: str) -> Optional[str]:
    if not header_value:
        return None
    parts = header_value.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    if _looks_like_jwt(header_value):
        return header_value
    return None


@retry(
    reraise=True,
    stop=stop_after_attempt(config.RETRY_ATTEMPTS),
    wait=wait_exponential(min=config.RETRY_MIN_WAIT, max=config.RETRY_MAX_WAIT),
    retry=retry_if_exception_type(AuthError),
)
def get_access_token() -> str:
    """Log in and return just the Bearer token (browser session closes)."""
    token, _, _ = _login_session(do_export=False)
    return token


@retry(
    reraise=True,
    stop=stop_after_attempt(config.RETRY_ATTEMPTS),
    wait=wait_exponential(min=config.RETRY_MIN_WAIT, max=config.RETRY_MAX_WAIT),
    retry=retry_if_exception_type(AuthError),
)
def login_and_export() -> str:
    """
    Log in, select the business/location, run the export inside the browser,
    download the CSV inside the browser too (the S3 link needs the session
    context), and return the LOCAL path to the downloaded CSV file.
    """
    _, _, csv_path = _login_session(do_export=True)
    if not csv_path:
        raise AuthError("Export did not produce a CSV file")
    return csv_path


def _login_session(do_export: bool):
    """
    Drive the browser: login -> select business -> capture token -> (optional)
    in-page export + in-browser CSV download.
    Returns (token, csv_url_or_None, csv_path_or_None).
    """
    log.info("Launching browser for CRM login")
    captured_token: dict = {"value": None}
    csv_url: dict = {"value": None}
    csv_path: dict = {"value": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=config.BROWSER_HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page = context.new_page()

        def _on_request(request):
            # Fallback path: sniff a Bearer header off any outbound request.
            if captured_token["value"]:
                return
            auth_header = request.headers.get("authorization")
            tok = _extract_from_authorization(auth_header) if auth_header else None
            if tok:
                captured_token["value"] = tok

        def _on_response(response):
            # Capture the access_token from /clients/switch, and the S3 CSV
            # URL from the export response (whichever endpoint returns it).
            try:
                url = response.url
                if "bookeeapp.com/api" in url:
                    log.info(f"[debug] saw API response: {response.status} {url}")
                if "clients/switch" in url and response.status == 200:
                    data = response.json()
                    tok = _search_json_for_key(data, "access_token")
                    if tok:
                        captured_token["value"] = tok
                        log.info("[debug] captured access_token from clients/switch")
                # The export request returns JSON containing the temporary S3
                # CSV link. Grab it from the bookings export response, or from
                # any response that carries an S3 URL.
                if response.status == 200 and (
                    "reports/advanced/bookings" in url or "export" in url
                ):
                    try:
                        data = response.json()
                        link = _search_json_for_key(data, "url")
                        if link and str(link).lower().startswith("http"):
                            csv_url["value"] = link
                            log.info("[debug] captured CSV URL from export response")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass

        page.on("request", _on_request)
        page.on("response", _on_response)

        # Intercept the export POST and rewrite its body so the filter always
        # requests ALL data (every date), regardless of what the report page
        # has set in its UI. This guarantees the full dataset without needing
        # to click filter controls.
        def _route_export(route):
            try:
                req = route.request
                if (
                    req.method == "POST"
                    and "reports/advanced/bookings" in req.url
                    and "export=true" in req.url
                ):
                    payload = config.get_export_payload()
                    log.info("[debug] rewriting export payload to all-data filter")
                    route.continue_(post_data=json.dumps(payload))
                    return
            except Exception:  # noqa: BLE001
                pass
            route.continue_()

        try:
            page.route("**/reports/advanced/bookings?export=true*", _route_export)
        except Exception:  # noqa: BLE001
            pass

        try:
            page.goto(
                config.CRM_BASE_URL,
                wait_until="domcontentloaded",
                timeout=config.BROWSER_TIMEOUT_MS,
            )

            # Fill credentials. Selectors cover common Kenko login variants.
            _fill_first(
                page,
                [
                    "input[type='email']",
                    "input[name='email']",
                    "input[name='username']",
                    "input[autocomplete='username']",
                    "input[placeholder*='@']",
                ],
                config.CRM_USERNAME,
            )
            _fill_first(
                page,
                [
                    "input[type='password']",
                    "input[name='password']",
                    "input[autocomplete='current-password']",
                ],
                config.CRM_PASSWORD,
            )

            log.info("[debug] credentials filled, clicking sign in")
            _click_first(
                page,
                [
                    "button:has-text('Sign in')",
                    "button[type='submit']",
                    "button:has-text('Sign In')",
                    "button:has-text('Log in')",
                    "button:has-text('Login')",
                ],
            )
            # Fallback: many login forms also submit on Enter in the password
            # field. Harmless if the click already worked.
            try:
                page.keyboard.press("Enter")
            except Exception:  # noqa: BLE001
                pass

            # Let the post-login request fire and the SPA navigate away from
            # the login screen. Poll for the URL to leave /login (up to ~15s)
            # instead of a fixed 2s wait, which was often too short.
            left_login = False
            for _ in range(15):
                page.wait_for_timeout(1000)
                if "/login" not in page.url:
                    left_login = True
                    break
            _settle(page)
            log.info(f"[debug] after login, current URL: {page.url}")
            if not left_login:
                # Still on /login → look for a visible error message to log it.
                try:
                    err = page.locator(
                        "text=/incorrect|invalid|wrong|error|ไม่ถูกต้อง/i"
                    )
                    if err.count():
                        log.error(
                            f"[debug] login page error text: {err.first.inner_text()[:200]}"
                        )
                except Exception:  # noqa: BLE001
                    pass

            # Step 2: ALWAYS select the location ("Pilar Studio") so the SPA
            # navigates into crm.gokenko.com/calendar. This step is required:
            # jumping straight to the report URL bounces back to the chooser.
            # (We do this even if a token was already captured, because the
            # navigation into the dashboard is what unlocks the report page.)
            log.info("[debug] selecting location to enter dashboard")
            clicked = _select_location_and_wait(page)
            log.info(f"[debug] location selected & entered calendar: {clicked}")

            if not captured_token["value"]:
                captured_token["value"] = _scan_storage_for_jwt(page)

            # Give SPA a moment to fire authenticated XHRs if still empty.
            if not captured_token["value"]:
                page.wait_for_timeout(3000)
                captured_token["value"] = _scan_storage_for_jwt(page)

            # Run the export from inside the page so the WAF token + cookies
            # are attached by the browser automatically (a plain requests call
            # is blocked by AWS WAF with 403).
            if do_export and captured_token["value"]:
                export_url = _run_in_page_export(
                    page, captured_token["value"], csv_url
                )
                # Download the CSV inside the browser too: the S3 link may also
                # require the session context (a plain requests.get gets 403).
                csv_path["value"] = _download_csv_in_browser(page, export_url)

            # Save a screenshot when we still failed, to aid diagnosis.
            if not captured_token["value"]:
                try:
                    import os
                    os.makedirs(config.LOGS_DIR, exist_ok=True)
                    shot = os.path.join(config.LOGS_DIR, "login_debug.png")
                    page.screenshot(path=shot, full_page=True)
                    log.info(f"[debug] saved screenshot to {shot}")
                    log.info(f"[debug] final URL: {page.url}")
                except Exception:  # noqa: BLE001
                    pass

        except Exception as exc:  # noqa: BLE001
            log.error(f"Authentication failed: {exc}")
            try:
                import os
                os.makedirs(config.LOGS_DIR, exist_ok=True)
                page.screenshot(
                    path=os.path.join(config.LOGS_DIR, "login_error.png"),
                    full_page=True,
                )
            except Exception:  # noqa: BLE001
                pass
            raise AuthError(str(exc)) from exc
        finally:
            context.close()
            browser.close()

    token = captured_token["value"]
    if not token:
        log.error("Authentication failed: no Bearer token could be extracted")
        raise AuthError("No Bearer token extracted after login")

    log.info("Login successful")
    log.info("Bearer token extracted")
    return token, csv_url["value"], csv_path["value"]


def _run_in_page_export(page, token: str, csv_url: dict):
    """
    Export with our own "all data" filter, but issue the request from inside
    the crm.gokenko.com reports page so it is same-origin (no CORS) and the
    browser supplies the AWS WAF token + cookies automatically.

    This combines both needs:
      - full control over the filter (we send 'has any value', all rows), and
      - passing the WAF/CORS checks (request originates from the real page).
    """
    # Step 1: navigate to the Bookings report page (cookies carry over the
    # gokenko.com domain, so no location chooser is needed).
    log.info("Navigating to reports page for export")
    reached = False
    for attempt in range(3):
        try:
            page.goto(
                config.REPORTS_URL,
                wait_until="domcontentloaded",
                timeout=config.BROWSER_TIMEOUT_MS,
            )
            page.wait_for_timeout(5000)
            _settle(page)
            log.info(f"[debug] reports page URL: {page.url}")
            if "crm.gokenko.com" in page.url and "reports" in page.url:
                reached = True
                break
            page.wait_for_timeout(2000)
        except Exception as exc:  # noqa: BLE001
            log.info(f"[debug] reports navigation attempt {attempt+1}: {exc}")
            page.wait_for_timeout(2000)

    if not reached:
        log.info("[debug] could not confirm reports page; trying fetch anyway")

    # Step 2: issue the export request from the page (same origin as the export
    # API call the web app makes), with our full-data payload.
    log.info("Export API called (in-page, full-data filter)")
    payload = config.get_export_payload()
    endpoint = config.EXPORT_ENDPOINT

    js = """
    async ([endpoint, payload, token]) => {
      try {
        const res = await fetch(endpoint, {
          method: 'POST',
          credentials: 'include',
          headers: {
            'Accept': 'application/x.gymday.v1+json',
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token
          },
          body: JSON.stringify(payload)
        });
        const text = await res.text();
        return { status: res.status, body: text };
      } catch (e) {
        return { status: -1, body: String(e) };
      }
    }
    """
    try:
        result = page.evaluate(js, [endpoint, payload, token])
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"In-page export call failed: {exc}") from exc

    status = result.get("status")
    body = result.get("body", "")

    # If the direct fetch was blocked, fall back to clicking the Download
    # button on the page (uses the page's own filters/token).
    if status != 200:
        log.info(f"[debug] in-page fetch returned {status}; trying Download button")
        return _click_download_fallback(page, csv_url)

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthError(f"Export response was not JSON: {body[:200]}") from exc

    url = data.get("url")
    if not url and isinstance(data.get("data"), dict):
        url = data["data"].get("url")
    if not url:
        url = _search_json_for_key(data, "url")
    if not url or not str(url).lower().startswith("http"):
        raise AuthError(f"No valid CSV URL in export response: {str(data)[:200]}")

    log.info("CSV URL received")
    csv_url["value"] = url
    return url


def _click_download_fallback(page, csv_url: dict):
    """
    Fallback: click the page's real Download button and capture the resulting
    S3 URL from the response listener. Note: this uses whatever filter the page
    currently has set.
    """
    log.info("Export API called (clicking Download fallback)")
    # Clear any URL captured earlier (the report page auto-fires an export on
    # load). We want the FRESH url produced by this Download click, since S3
    # links are single-use / short-lived.
    csv_url["value"] = None
    download_selectors = [
        "span:has-text('Download')",
        "text=Download",
        "button:has-text('Download')",
        "[role='button']:has-text('Download')",
        "*:has-text('Download')",
    ]
    clicked = False
    for sel in download_selectors:
        try:
            el = page.wait_for_selector(sel, timeout=8000, state="visible")
        except Exception:  # noqa: BLE001
            el = None
        if el:
            try:
                el.click()
                clicked = True
                log.info(f"[debug] clicked Download via selector: {sel}")
                break
            except Exception:  # noqa: BLE001
                continue

    if not clicked:
        try:
            import os
            os.makedirs(config.LOGS_DIR, exist_ok=True)
            page.screenshot(
                path=os.path.join(config.LOGS_DIR, "reports_debug.png"),
                full_page=True,
            )
            log.info("[debug] saved reports_debug.png (Download not found)")
        except Exception:  # noqa: BLE001
            pass
        raise AuthError("Could not find the Download button on the report page")

    # Wait for the FRESH export URL from this click.
    for _ in range(30):
        if csv_url["value"]:
            break
        page.wait_for_timeout(1000)

    if not csv_url["value"]:
        raise AuthError("Download clicked but no CSV URL was captured")

    # Small settle so the captured URL is the final one.
    page.wait_for_timeout(1500)
    log.info("CSV URL received")
    return csv_url["value"]


def _download_csv_in_browser(page, url: str) -> str:
    """
    Download the CSV from the (S3 presigned) URL and save to temp/bookings.csv.

    S3 presigned URLs are self-authorizing, so a plain HTTP GET works and is
    the most reliable path. (A browser fetch() to S3 is blocked by CORS since
    it's a different origin than crm.gokenko.com.) We therefore use requests
    here, with the freshly-captured URL.
    """
    import os
    import requests

    log.info("Downloading CSV")
    try:
        from urllib.parse import urlparse
        log.info(f"[debug] CSV host: {urlparse(url).netloc}")
    except Exception:  # noqa: BLE001
        pass
    os.makedirs(config.TEMP_DIR, exist_ok=True)

    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=config.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last_err = f"network error: {exc}"
            continue
        if resp.status_code == 200 and resp.content:
            with open(config.CSV_PATH, "wb") as fh:
                fh.write(resp.content)
            log.info("CSV downloaded")
            return config.CSV_PATH
        last_err = f"status {resp.status_code}"
        # brief backoff
        page.wait_for_timeout(1500)

    raise AuthError(f"CSV download failed ({last_err})")


def _settle(page, timeout_ms: int = 5000) -> None:
    """
    Give the page a brief chance to settle. The Kenko dashboard polls APIs
    continuously, so 'networkidle' may never fire; we cap the wait at a few
    seconds and move on regardless instead of blocking indefinitely.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        # Never idle within the cap — that's fine, proceed.
        pass


def _select_location_and_wait(page) -> bool:
    """
    Click the Pilar Studio LOCATION on the chooser and wait until the SPA
    actually navigates into crm.gokenko.com (the calendar/dashboard). The
    location chooser must be used; jumping straight to a report URL bounces
    back here. Tries several click targets and verifies via URL change.
    """
    name = config.CRM_BUSINESS_NAME

    # First make sure the business on the left is selected (best effort).
    for sel in [f"div:has-text('{name}')", f"text={name}"]:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click(timeout=4000)
                page.wait_for_timeout(1200)
                break
        except Exception:  # noqa: BLE001
            continue

    # Candidate click targets for the LOCATION row (right-hand list). The real
    # clickable element is a div with class 'cursor-pointer' that contains the
    # studio name (confirmed via DevTools). We target that first.
    candidate_selectors = [
        # Most specific: the confirmed location row (h-16 + cursor-pointer).
        f"div.h-16.cursor-pointer:has-text('{name}')",
        f"div.cursor-pointer:has-text('{name}')",
        f"div[class*='cursor-pointer']:has-text('{name}')",
        f"div:has-text('{name}'):has-text('{config.CRM_REGION_NAME}')",
        f"[role='button']:has-text('{name}')",
        f"li:has-text('{name}')",
        f"a:has-text('{name}')",
        f"div:has-text('{name}')",
        f"text={name}",
    ]

    for sel in candidate_selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
        except Exception:  # noqa: BLE001
            count = 0
        if not count:
            continue
        targets = [loc.last, loc.first] if count > 1 else [loc.first]
        for target in targets:
            try:
                target.scroll_into_view_if_needed(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            try:
                target.click(timeout=5000)
            except Exception:  # noqa: BLE001
                continue
            # Wait up to ~12s for navigation into crm.gokenko.com.
            for _ in range(12):
                page.wait_for_timeout(1000)
                if "crm.gokenko.com" in page.url:
                    log.info(f"[debug] entered dashboard via selector: {sel}")
                    _settle(page)
                    return True

    log.info("[debug] location click did not reach crm.gokenko.com")
    return False


def _try_select_business(page) -> bool:
    """
    Handle Kenko's two-step chooser:
      1. (Left) "Your Business Accounts" -> click the business (e.g. Pilar Studio).
      2. (Right) "Select your location" -> click the LOCATION row, which is the
         entry showing the studio name with its region as a subtitle (NOT the
         "Region" row). Clicking the location enters the dashboard and fires
         the second /clients/switch that yields the export-capable token.
    Returns True if it reached/clicked a location.
    """
    name = config.CRM_BUSINESS_NAME
    region = config.CRM_REGION_NAME

    # Step 1: click the business on the left (best-effort; may already be set).
    for sel in [
        f"div:has-text('{name}')",
        f"button:has-text('{name}')",
        f"text={name}",
    ]:
        try:
            el = page.wait_for_selector(sel, timeout=5000, state="visible")
        except PlaywrightTimeoutError:
            el = None
        if el:
            try:
                el.click()
                page.wait_for_timeout(1500)
                break
            except Exception:  # noqa: BLE001
                continue

    # Step 2: click the LOCATION row on the right. The location entry shows the
    # studio name (sometimes with the region as a subtitle). We try the most
    # specific selectors first, then progressively looser ones. We pick the
    # LAST match because the right-hand location list renders after the
    # left-hand account box.
    location_selectors = [
        # Most specific: a row containing both studio name and region subtitle.
        f"div:has-text('{name}'):has-text('{region}')",
        f"li:has-text('{name}'):has-text('{region}')",
        # A clickable row that has the studio name (right-hand list item).
        f"[role='button']:has-text('{name}')",
        f"li:has-text('{name}')",
        f"a:has-text('{name}')",
        # Looser fallbacks.
        f"div:has-text('{name}')",
        f"text={name}",
        f"*:has-text('{name}')",
    ]
    for sel in location_selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
        except Exception:  # noqa: BLE001
            count = 0
        if not count:
            continue
        # Try the last match first (right-hand location), then the first.
        for target in ([loc.last, loc.first] if count > 1 else [loc.first]):
            try:
                target.scroll_into_view_if_needed(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            try:
                target.click(timeout=5000)
                log.info(f"[debug] clicked location via selector: {sel}")
                page.wait_for_timeout(1500)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _search_json_for_key(obj, key: str) -> Optional[str]:
    """Recursively find the first string value stored under `key`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str) and v:
                return v
            found = _search_json_for_key(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _search_json_for_key(v, key)
            if found:
                return found
    return None


def _fill_first(page, selectors, value) -> None:
    # Wait for the first matching field to actually render. The Kenko login
    # is a client-rendered SPA, so fields may not exist on first paint; a
    # plain query_selector would return None too early. We give the union of
    # selectors up to BROWSER_TIMEOUT_MS to appear, then fill whichever hits.
    deadline_each = max(int(config.BROWSER_TIMEOUT_MS / max(len(selectors), 1)), 3000)
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=deadline_each, state="visible")
        except PlaywrightTimeoutError:
            el = None
        if el:
            # Kenko's login is a React SPA. A bare .fill() sets the DOM value
            # but does not always fire React's onChange, so the component state
            # stays empty and the Sign-in button submits blank credentials.
            # Clear, focus, then type character-by-character to fire the input
            # events React listens for; finally dispatch input+change to be safe.
            el.click()
            el.fill("")
            el.type(value, delay=30)
            try:
                el.dispatch_event("input")
                el.dispatch_event("change")
            except Exception:  # noqa: BLE001
                pass
            return
    raise AuthError(f"None of the input selectors matched: {selectors}")


def _click_first(page, selectors) -> None:
    deadline_each = max(int(config.BROWSER_TIMEOUT_MS / max(len(selectors), 1)), 3000)
    for sel in selectors:
        try:
            el = page.wait_for_selector(sel, timeout=deadline_each, state="visible")
        except PlaywrightTimeoutError:
            el = None
        if el:
            el.click()
            return
    raise AuthError(f"None of the submit selectors matched: {selectors}")


def _scan_storage_for_jwt(page) -> Optional[str]:
    """Scan localStorage and sessionStorage values for a JWT-looking token."""
    script = """
    () => {
      const out = [];
      for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i++) {
          const k = store.key(i);
          out.push(store.getItem(k));
        }
      }
      return out;
    }
    """
    try:
        values = page.evaluate(script)
    except Exception:  # noqa: BLE001
        return None

    for raw in values:
        if not raw:
            continue
        candidate = raw.strip().strip('"')
        if _looks_like_jwt(candidate):
            return candidate
        # Token may be nested inside a JSON blob.
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        found = _search_json_for_jwt(obj)
        if found:
            return found
    return None


def _search_json_for_jwt(obj) -> Optional[str]:
    if isinstance(obj, str) and _looks_like_jwt(obj):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            found = _search_json_for_jwt(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _search_json_for_jwt(v)
            if found:
                return found
    return None
