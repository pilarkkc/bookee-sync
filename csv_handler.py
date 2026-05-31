"""
csv_handler.py
--------------
Downloads the exported CSV from the temporary S3 URL and parses it with
pandas. Columns are read dynamically: names and order are preserved exactly
as exported and are never hardcoded.
"""

import os

import pandas as pd
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


class CSVError(Exception):
    """Raised when download or parsing of the CSV fails."""


class _RetryableDownloadError(CSVError):
    """Transient download error eligible for retry."""


@retry(
    reraise=True,
    stop=stop_after_attempt(config.RETRY_ATTEMPTS),
    wait=wait_exponential(min=config.RETRY_MIN_WAIT, max=config.RETRY_MAX_WAIT),
    retry=retry_if_exception_type(_RetryableDownloadError),
)
def download_csv(url: str) -> str:
    """Download the CSV to temp/bookings.csv with validation."""
    os.makedirs(config.TEMP_DIR, exist_ok=True)

    try:
        resp = requests.get(url, timeout=config.HTTP_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise _RetryableDownloadError(f"Network error downloading CSV: {exc}") from exc

    if resp.status_code in (500, 502, 503, 504):
        raise _RetryableDownloadError(f"Transient S3 status {resp.status_code}")

    if not resp.ok:
        raise CSVError(f"CSV download failed with status {resp.status_code}")

    with open(config.CSV_PATH, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                fh.write(chunk)

    if not os.path.exists(config.CSV_PATH):
        raise CSVError("CSV file was not created on disk")

    size = os.path.getsize(config.CSV_PATH)
    if size <= 0:
        raise CSVError("Downloaded CSV file is empty (size 0)")

    log.info("CSV downloaded")
    return config.CSV_PATH


def parse_csv(path: str) -> pd.DataFrame:
    """
    Parse the CSV into a DataFrame.

    - Reads all columns dynamically (no column names hardcoded).
    - Preserves exact column order and names.
    - Supports UTF-8 and UTF-8 BOM (utf-8-sig handles both).
    - Treats every value as a string to avoid type coercion / data loss.
    - Drops fully blank rows.
    - Validates the frame is not empty.
    """
    # Bookee exports have a trailing comma on every data row, producing one
    # extra empty field per row (27 fields vs 26-column header).  Reading the
    # header column count first and passing usecols=range(n) tells pandas to
    # keep only the real columns and discard the phantom trailing field —
    # no ParserWarning, no column shift.
    import csv as _csv

    enc = "utf-8-sig"
    try:
        with open(path, encoding=enc) as _f:
            _n_cols = len(next(_csv.reader(_f)))
    except UnicodeDecodeError:
        enc = "utf-8"
        with open(path, encoding=enc) as _f:
            _n_cols = len(next(_csv.reader(_f)))

    try:
        df = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            encoding=enc,
            usecols=range(_n_cols),
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8",
            usecols=range(_n_cols),
        )

    # Remove rows where every cell is blank/whitespace.
    mask_non_blank = df.apply(
        lambda row: any(str(v).strip() != "" for v in row), axis=1
    )
    df = df[mask_non_blank].reset_index(drop=True)

    if df.empty:
        log.error("CSV empty")
        raise CSVError("Parsed DataFrame is empty after removing blank rows")

    columns = list(df.columns)
    log.info(f"Columns detected: {len(columns)}")
    log.info(f"Column names detected: {columns}")
    log.info(f"Rows imported: {len(df)}")
    return df
