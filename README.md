# Bookee → Google Sheets Sync

Automation that pulls booking data from the Bookee/Kenko CRM and mirrors it
into a Google Sheet every 10 minutes. The sheet is an exact mirror of the
exported CRM CSV — all columns, in order, are uploaded verbatim and the
worksheet is fully replaced on every run. No merge, incremental, or
duplicate logic.

## How it works

```
[preferred]  POST login API ──▶ Bearer token ─┐
[fallback]   Playwright login ─▶ Bearer token ─┤
                  ▲                             ▼
                  └──── re-auth on 401 ◀── Export API ─▶ S3 CSV URL
                                                ▼
                          download CSV ─▶ pandas parse ─▶ replace worksheet
```

Authentication tries the **direct login API first** (a plain credential POST,
no browser) and only falls back to **Playwright** if the API endpoint isn't
configured or the call fails. Either way the token lives in memory only and is
refreshed automatically when the export API returns `401`.

### Enabling direct API auth (recommended)

Browser automation is slower and more brittle than a direct call, so prefer
the API path. To find the real endpoint:

1. Open `crm.gokenko.com` in Chrome with DevTools → **Network**, filtered to
   **Fetch/XHR**.
2. Log in. Find the request whose **response JSON contains the token**.
3. From that request, read off four things and set them in `.env`:
   - `AUTH_LOGIN_URL` — the request URL
   - `AUTH_USERNAME_FIELD` / `AUTH_PASSWORD_FIELD` — the body field names
   - `AUTH_TOKEN_JSON_PATH` — dot-path to the token (e.g. `token`,
     `data.access_token`)
   - `AUTH_EXTRA_BODY_JSON` — any extra static body fields, if needed

With `AUTH_LOGIN_URL` set, the browser is never launched. Leave it blank to
use Playwright. No code changes are needed to switch between the two.

## Project layout

```
project/
  main.py            # orchestration entrypoint
  api_auth.py        # direct API login (preferred)
  auth.py            # Playwright login + Bearer token extraction (fallback)
  token_manager.py   # in-memory token cache; API-first, browser fallback
  export_api.py      # export API call, 401 auto re-login
  csv_handler.py     # download + pandas parse (dynamic columns)
  google_sheet.py    # clear + batched upload to worksheet
  config.py          # env config + configurable export payload
  logger.py          # shared logging (stdout + logs/ file)
  requirements.txt
  .env.example
  README.md
  .github/workflows/sync.yml
  logs/              # run logs
  temp/              # downloaded bookings.csv
```

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

## Environment setup

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `CRM_USERNAME` | CRM login email/username |
| `CRM_PASSWORD` | CRM password |
| `CENTER_ID` | Bookee center ID (default provided) |
| `GOOGLE_SHEET_ID` | Spreadsheet ID from its URL |
| `GOOGLE_WORKSHEET_NAME` | Tab name (default `Bookings`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Raw service-account JSON **or** a path to the JSON file |

Optional overrides (retries, timeouts, batch size, and a full
`EXPORT_PAYLOAD_JSON` to change filters/columns without code edits) are listed
in `.env.example`.

Run locally:

```bash
python main.py
```

## Google Service Account setup

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or pick an existing one).
2. Enable the **Google Sheets API** and **Google Drive API**.
3. Go to **APIs & Services → Credentials → Create credentials → Service
   account**. Create it and open the new account.
4. Under **Keys → Add key → Create new key → JSON**, download the JSON file.
5. Open your Google Sheet and **Share** it with the service account email
   (`...@...iam.gserviceaccount.com`) as an **Editor**.
6. Put the JSON into `GOOGLE_SERVICE_ACCOUNT_JSON` — either the file path
   (local) or the full JSON string (CI).

## GitHub Secrets setup

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add:

- `CRM_USERNAME`
- `CRM_PASSWORD`
- `CENTER_ID`
- `GOOGLE_SHEET_ID`
- `GOOGLE_WORKSHEET_NAME`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the entire JSON string)

## Deployment

Push the repo to GitHub. The workflow in `.github/workflows/sync.yml` runs
automatically every 10 minutes (`*/10 * * * *`) and can also be triggered
manually via **Actions → Bookee Sheet Sync → Run workflow**. Logs are uploaded
as a build artifact after every run.

> Note: GitHub's scheduled triggers are best-effort and can be delayed under
> load. For strict 10-minute precision, run the same `main.py` from a cron job
> on a small VM.

## Troubleshooting

- **No Bearer token extracted** — the login form selectors may have changed.
  Inspect `auth.py` selectors, or check whether the account requires MFA.
- **Export API 401 loops** — verify credentials; a persistent 401 after
  re-login usually means bad username/password.
- **`No 'url' field in export response`** — the export payload/filters may be
  rejected. Adjust filters via `EXPORT_PAYLOAD_JSON`.
- **CSV empty** — the filter returned no rows; confirm the Location/Event Date
  filters match real data.
- **Google Sheet upload failed / 403** — the sheet isn't shared with the
  service account email, or the Sheets/Drive APIs aren't enabled.
- **Quota / 429 errors** — increase `SHEET_BATCH_ROWS` to reduce the number of
  write calls.
- **Playwright errors in CI** — ensure the `playwright install --with-deps
  chromium` step ran; it's already in the workflow.

## Security

Credentials, tokens, and the service-account key are read only from
environment variables / GitHub Secrets. Nothing is hardcoded. Keep `.env` and
any downloaded JSON key out of version control.
