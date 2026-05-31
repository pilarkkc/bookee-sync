"""
main.py
-------
Orchestrates a single synchronisation run:

  1. Obtain a Bearer token (lazy login).
  2. Call the export API -> temporary S3 CSV URL (auto re-login on 401).
  3. Download the CSV.
  4. Parse the CSV (dynamic columns).
  5. Replace the entire Google Sheet worksheet.

Scheduling (every 10 minutes) is handled externally by GitHub Actions, so
this script runs once per invocation and exits with code 0 on success or 1
on failure. Each stage has its own retry/backoff; a clean run targets
under ~15 seconds for 3,000-10,000 rows (excluding cold browser login).
"""

import sys
import time

import csv_handler
import export_api
import google_sheet
from logger import get_logger

log = get_logger()


def run() -> int:
    start = time.time()
    try:
        path = export_api.get_csv_file()
        df = csv_handler.parse_csv(path)
        google_sheet.update_sheet(df)

        elapsed = time.time() - start
        log.info(f"Sync completed successfully in {elapsed:.2f}s")
        return 0
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - start
        log.error(f"Sync failed after {elapsed:.2f}s: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run())
