"""
config.py
---------
Centralized configuration loaded from environment variables.
Also builds the configurable export payload (no hardcoded report columns).
"""

import json
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


# --- Credentials -----------------------------------------------------------
CRM_USERNAME = _require("CRM_USERNAME")
CRM_PASSWORD = _require("CRM_PASSWORD")

# --- CRM / Export ----------------------------------------------------------
CENTER_ID = os.getenv("CENTER_ID", "7ab7e537-a702-43b5-bf82-1b7efe2d8e0f")
CRM_BUSINESS_NAME = os.getenv("CRM_BUSINESS_NAME", "Pilar Studio")
CRM_REGION_NAME = os.getenv("CRM_REGION_NAME", "KHONKAEN")
CRM_BASE_URL = os.getenv("CRM_BASE_URL", "https://crm.gokenko.com")
# The Bookings report page (same browser context the web app exports from).
REPORTS_URL = os.getenv(
    "REPORTS_URL",
    "https://crm.gokenko.com/#/client/reports/detailed-reports?mode=bookings",
)
EXPORT_BASE_URL = os.getenv("EXPORT_BASE_URL", "https://data.bookeeapp.com")

EXPORT_ENDPOINT = (
    f"{EXPORT_BASE_URL}/api/reports/advanced/bookings"
    f"?export=true&center_id={CENTER_ID}"
)

EXPORT_HEADERS = {
    "Accept": "application/x.gymday.v1+json",
    "Content-Type": "application/json",
    "Origin": CRM_BASE_URL,
    "Referer": f"{CRM_BASE_URL}/",
}

# --- Direct API authentication (preferred over Playwright) -----------------
# If AUTH_LOGIN_URL is set, the system POSTs credentials directly to that
# endpoint and reads the token out of the JSON response, skipping the browser.
# Leave it blank to fall straight through to Playwright.
#
# Find the real values via Chrome DevTools (Network -> XHR) at login:
#   - AUTH_LOGIN_URL:       the request URL that returns the JWT
#   - AUTH_TOKEN_JSON_PATH: dot-path to the token in the response JSON
#                           (e.g. "token", "data.access_token", "auth.jwt")
#   - AUTH_USERNAME_FIELD / AUTH_PASSWORD_FIELD: body field names
AUTH_LOGIN_URL = os.getenv("AUTH_LOGIN_URL", "").strip()
AUTH_USERNAME_FIELD = os.getenv("AUTH_USERNAME_FIELD", "email")
AUTH_PASSWORD_FIELD = os.getenv("AUTH_PASSWORD_FIELD", "password")
AUTH_TOKEN_JSON_PATH = os.getenv("AUTH_TOKEN_JSON_PATH", "token")

# Step 2 (Kenko/Bookee): after login, switch to the selected business/client
# to obtain the real access token. Leave AUTH_SWITCH_URL or AUTH_CLIENT_ID
# blank to skip and use the login token directly.
AUTH_SWITCH_URL = os.getenv("AUTH_SWITCH_URL", "").strip()
AUTH_CLIENT_ID = os.getenv("AUTH_CLIENT_ID", "").strip()
AUTH_CLIENT_ID_FIELD = os.getenv("AUTH_CLIENT_ID_FIELD", "client_id")
AUTH_SWITCH_TOKEN_JSON_PATH = os.getenv(
    "AUTH_SWITCH_TOKEN_JSON_PATH", "data.user.access_token"
)
# Extra static JSON fields to include in the login body, as a JSON object
# string, e.g. '{"center_id":"..."}'. Optional.
AUTH_EXTRA_BODY_JSON = os.getenv("AUTH_EXTRA_BODY_JSON", "").strip()


def get_auth_extra_body() -> dict:
    if AUTH_EXTRA_BODY_JSON:
        return json.loads(AUTH_EXTRA_BODY_JSON)
    return {}


# --- Google Sheets ---------------------------------------------------------
GOOGLE_SHEET_ID = _require("GOOGLE_SHEET_ID")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Bookings")
# Service account JSON may be supplied as a raw JSON string OR a file path.
GOOGLE_SERVICE_ACCOUNT_JSON = _require("GOOGLE_SERVICE_ACCOUNT_JSON")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# --- Paths -----------------------------------------------------------------
TEMP_DIR = os.getenv("TEMP_DIR", "temp")
LOGS_DIR = os.getenv("LOGS_DIR", "logs")
CSV_PATH = os.path.join(TEMP_DIR, "bookings.csv")

# --- Retry / timeouts ------------------------------------------------------
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_MIN_WAIT = float(os.getenv("RETRY_MIN_WAIT", "2"))
RETRY_MAX_WAIT = float(os.getenv("RETRY_MAX_WAIT", "20"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "60000"))
# Set BROWSER_HEADLESS=false in .env to watch the browser while debugging.
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "true").strip().lower() != "false"

# --- Google Sheets batching ------------------------------------------------
SHEET_BATCH_ROWS = int(os.getenv("SHEET_BATCH_ROWS", "5000"))


def _default_payload() -> dict:
    """
    Real Kenko export payload (captured from the live web app).

    Filters:
      - event_date  = has any value (all dates)
      - location_name in [1995]  (Pilar Studio's numeric location id)
    Sort: event_date DESC.

    Columns and filters can be overridden without code changes by setting
    EXPORT_PAYLOAD_JSON in the environment.
    """
    return {
        "columns": [
            "location_name", "booking_date", "event_date", "booking_status",
            "event_name", "event_type", "duration", "instructors",
            "facility_name", "contact_name", "contact_email",
            "booked_by_friend", "guest_pass_applied", "booking_source",
            "cancelation_date", "all_booking_cancelation_type",
            "cancelation_charge", "cancelation_source", "attendance_date",
            "all_bookings_checkin_status", "noshow_charge_type",
            "noshow_charge_amount", "all_bookings_payment_status",
            "membership_name", "expiry_date", "credits",
        ],
        "filters": {
            "condition": "and",
            "rules": [
                {"field": "event_date", "operator": "has any value",
                 "value": ""},
                {"field": "location_name", "operator": "in", "value": [1995]},
            ],
        },
        "sort_by": "event_date",
        "sort_direction": "desc",
    }


def get_export_payload() -> dict:
    """
    Returns the export payload.

    If EXPORT_PAYLOAD_JSON (env) is set, it is parsed and used verbatim,
    enabling future filter/column modifications with zero code changes.
    Otherwise the configurable default is returned.
    """
    raw = os.getenv("EXPORT_PAYLOAD_JSON")
    if raw:
        return json.loads(raw)
    return _default_payload()
