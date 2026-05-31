"""
export_api.py
-------------
Obtains the temporary S3 CSV URL from the Bookee export endpoint.

The export endpoint sits behind AWS WAF, which blocks plain server-side
requests (403). So the export is performed from inside the authenticated
browser session (see auth.login_and_export), which carries the WAF token and
cookies automatically. This module is a thin wrapper around that.
"""

import auth
from logger import get_logger

log = get_logger()


class ExportAPIError(Exception):
    """Raised when the export call fails."""


def get_csv_file() -> str:
    """Log in via the browser, export, and download the CSV; return its path."""
    try:
        return auth.login_and_export()
    except auth.AuthError as exc:
        raise ExportAPIError(str(exc)) from exc
