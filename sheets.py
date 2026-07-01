#!/usr/bin/env python3
"""
sheets.py  —  Google Sheets writer for the World Cup closing-line collector.

Appends one row per match to a Google Sheet, idempotently: it reads the existing
`match_id` column first and skips any match already present, so re-runs never
duplicate rows. Header is written automatically on first use.

CREDENTIALS
-----------
Set GOOGLE_SERVICE_ACCOUNT_JSON to EITHER:
  * the raw JSON of a service-account key, OR
  * a filesystem path to that key file.

Share your target Google Sheet with the service account's client_email
(Editor access), and put the sheet id in WC_SHEET_ID.

This module is imported lazily by wc_collector.py only when WC_SHEET_ID is set,
so the collector runs fine (JSON/CSV only) even if gspread isn't installed.
"""

import json
import os


def _load_credentials():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")

    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if os.path.exists(raw):
        return Credentials.from_service_account_file(raw, scopes=scopes)
    info = json.loads(raw)
    return Credentials.from_service_account_info(info, scopes=scopes)


def make_sheet_writer(sheet_id, tab_name, fieldnames):
    """Return a callable(new_rows: list[dict]) -> int (number of rows actually
    appended). Raises if credentials / gspread are unavailable so the caller can
    fall back to JSON/CSV cleanly."""
    import gspread  # imported here so the collector doesn't hard-depend on it

    creds = _load_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    # Get or create the worksheet/tab.
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=200, cols=max(26, len(fieldnames)))

    # Ensure header row.
    existing = ws.get_all_values()
    if not existing:
        ws.update([fieldnames], "A1")
        existing = [fieldnames]

    header = existing[0]
    # Map by header so column order in the sheet is authoritative even if it was
    # edited by hand.
    try:
        id_col = header.index("match_id")
    except ValueError:
        id_col = 0

    def _existing_ids():
        vals = ws.get_all_values()
        return {r[id_col] for r in vals[1:] if len(r) > id_col and r[id_col]}

    def writer(new_rows):
        have = _existing_ids()
        payload = []
        for row in new_rows:
            mid = str(row.get("match_id", ""))
            if mid and mid in have:
                continue
            payload.append([_cell(row.get(col)) for col in header])
            have.add(mid)
        if payload:
            ws.append_rows(payload, value_input_option="USER_ENTERED")
        return len(payload)

    return writer


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return v
