"""
google_sheet.py
---------------
Replaces the entire contents of a worksheet with the exported CSV data.
The sheet schema always follows the CSV schema (headers + all rows) with no
custom mapping. Uses batched updates to minimise API calls for large frames.
"""

import json
import math
import os

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

import config
from logger import get_logger

log = get_logger()


class SheetError(Exception):
    """Raised when the Google Sheet update fails."""


def _load_credentials() -> Credentials:
    """
    Load service-account credentials. GOOGLE_SERVICE_ACCOUNT_JSON may be either
    a raw JSON string (e.g. a GitHub Secret) or a path to a JSON file.
    """
    raw = config.GOOGLE_SERVICE_ACCOUNT_JSON
    info = None

    if os.path.exists(raw):
        with open(raw, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    else:
        info = json.loads(raw)

    return Credentials.from_service_account_info(info, scopes=config.GOOGLE_SCOPES)


def _column_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA ... for building A1 ranges."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _open_worksheet():
    creds = _load_credentials()
    log.info(f"[debug] authorized as: {getattr(creds, 'service_account_email', '?')}")
    client = gspread.authorize(creds)
    log.info("[debug] opening spreadsheet by key...")
    spreadsheet = client.open_by_key(config.GOOGLE_SHEET_ID)
    log.info(f"[debug] spreadsheet opened: {spreadsheet.title}")
    try:
        ws = spreadsheet.worksheet(config.GOOGLE_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        log.info(f"[debug] worksheet '{config.GOOGLE_WORKSHEET_NAME}' not found; "
                 f"creating it")
        ws = spreadsheet.add_worksheet(
            title=config.GOOGLE_WORKSHEET_NAME, rows=1000, cols=26
        )
    return ws


@retry(
    reraise=True,
    stop=stop_after_attempt(config.RETRY_ATTEMPTS),
    wait=wait_exponential(min=config.RETRY_MIN_WAIT, max=config.RETRY_MAX_WAIT),
    retry=retry_if_exception_type((gspread.exceptions.APIError, SheetError)),
)
def update_sheet(df: pd.DataFrame) -> None:
    """
    Clear the worksheet and upload headers + all rows.

    Values are written in batches (config.SHEET_BATCH_ROWS rows per call) to
    keep the number of API requests minimal even for 10,000+ rows.
    """
    try:
        log.info(f"[debug] opening sheet id={config.GOOGLE_SHEET_ID} "
                 f"worksheet={config.GOOGLE_WORKSHEET_NAME}")
        ws = _open_worksheet()
        log.info("[debug] worksheet opened OK")

        header = list(df.columns)
        rows = df.astype(str).values.tolist()
        total_rows = len(rows) + 1  # + header
        total_cols = len(header)

        # Ensure the grid is large enough, then clear existing contents.
        ws.resize(rows=max(total_rows, 1), cols=max(total_cols, 1))
        ws.clear()

        last_col = _column_letter(total_cols)

        # Write header at row 1.
        ws.update(
            range_name=f"A1:{last_col}1",
            values=[header],
            value_input_option="RAW",
        )

        # Write data rows in batches.
        batch = config.SHEET_BATCH_ROWS
        num_batches = math.ceil(len(rows) / batch) if rows else 0
        for i in range(num_batches):
            chunk = rows[i * batch : (i + 1) * batch]
            start_row = 2 + (i * batch)  # row 1 is the header
            end_row = start_row + len(chunk) - 1
            ws.update(
                range_name=f"A{start_row}:{last_col}{end_row}",
                values=chunk,
                value_input_option="RAW",
            )

        log.info("Google Sheet updated")
    except gspread.exceptions.APIError as exc:
        log.error(f"Google Sheet upload failed (APIError): {exc}")
        raise
    except gspread.exceptions.SpreadsheetNotFound as exc:
        log.error(f"Google Sheet upload failed: spreadsheet not found "
                  f"(check GOOGLE_SHEET_ID and that the service account has "
                  f"access): {exc}")
        raise SheetError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.error(f"Google Sheet upload failed: {type(exc).__name__}: {exc}")
        raise SheetError(str(exc)) from exc
