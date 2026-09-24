"""Lead cards: written to the Google Sheets lead tracker (via an Apps Script web app) and a local CSV backup."""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

import httpx

from .prompt import SAST

SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "")
CSV_PATH = Path(os.getenv("LEADS_CSV", Path(__file__).resolve().parent.parent / "data" / "leads.csv"))

FIELDS = [
    "timestamp", "lead_id", "channel", "customer_name", "phone", "campaign",
    "make", "model", "year", "vin", "part", "side", "part_number", "photos",
    "preference", "delivery_town", "stock_result", "outcome", "handover_reason", "priority", "status",
]


def save_lead(card: dict) -> dict:
    row = {k: "" for k in FIELDS}
    row.update({k: (", ".join(v) if isinstance(v, list) else v) for k, v in card.items() if k in FIELDS})
    row["timestamp"] = row["timestamp"] or datetime.now(SAST).strftime("%Y-%m-%d %H:%M")
    row["status"] = row["status"] or "New"

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)

    sheet_ok = None
    if SHEETS_WEBHOOK_URL:
        try:
            r = httpx.post(SHEETS_WEBHOOK_URL, json=row, timeout=15, follow_redirects=True)
            sheet_ok = r.status_code < 400
        except httpx.HTTPError:
            sheet_ok = False
    return {"saved": True, "google_sheet": sheet_ok, "row": row}


def format_card(row: dict) -> str:
    """Private note for staff in the inbox."""
    lines = ["LEAD CARD"]
    labels = [("Customer", "customer_name"), ("Phone", "phone"), ("Channel", "channel"), ("Vehicle", None),
              ("VIN", "vin"), ("Part", "part"), ("Side", "side"), ("Part no.", "part_number"),
              ("Photos", "photos"), ("Preference", "preference"), ("Deliver to", "delivery_town"),
              ("Stock search", "stock_result"), ("Why handed over", "handover_reason")]
    for label, key in labels:
        if key is None:
            val = " ".join(str(row.get(k, "")) for k in ("make", "model", "year")).strip()
        else:
            val = row.get(key, "")
        if val:
            lines.append(f"{label}: {val}")
    return "\n".join(lines)
