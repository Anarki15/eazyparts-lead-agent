"""Win back old leads: send an approved WhatsApp template through Chatwoot to people whose part is now in stock.

Staff upload a CSV on /reactivate (lead_id, phone, name, part_text, price, variant_id, product, request),
preview it, send a test to their own number, then send to the ticked rows. Each customer gets one message:
the conversation is opened as "pending" so the bot answers their reply, and what we offered is saved
against their phone number so the bot knows the story. STOP replies are remembered and never messaged again.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from .agent import DB_PATH

log = logging.getLogger("eazyparts.reactivate")

TEMPLATE = os.getenv("REACTIVATE_TEMPLATE", "old_lead_part_in_stock")
TEMPLATE_LANG = os.getenv("REACTIVATE_TEMPLATE_LANG", "en")
WA_INBOX_ID = int(os.getenv("CHATWOOT_WA_INBOX_ID", "140426"))
DAILY_LIMIT = int(os.getenv("REACTIVATE_DAILY_LIMIT", "200"))  # Meta tier is 250 new chats / 24h; keep headroom
GAP_SECONDS = 2.0
STOP_WORDS = {"stop", "unsubscribe", "opt out", "optout", "stop please", "please stop"}

BODY = ("Hi {name}, a while ago you asked eAZyparts about a {part}. We now have one in stock that may fit, "
        "from R{price}. Reply YES and we'll confirm it fits your car and send the link. Reply STOP to opt out.")

_state = {"running": False, "log": []}
_lock = threading.Lock()


def _db():
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.execute("CREATE TABLE IF NOT EXISTS reactivation (phone TEXT PRIMARY KEY, lead_id TEXT, sent_at TEXT, "
               "conv_id TEXT, product TEXT, price TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS optouts (phone TEXT PRIMARY KEY, at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS lead_forms (phone TEXT PRIMARY KEY, data TEXT)")
    return db


def is_stop(text: str) -> bool:
    return (text or "").strip().strip(".!").lower() in STOP_WORDS


def record_optout(phone: str):
    if phone:
        db = _db()
        db.execute("REPLACE INTO optouts VALUES (?,?)", (phone, datetime.now(timezone.utc).isoformat()))
        db.commit()


def already_contacted(phone: str) -> str | None:
    db = _db()
    if db.execute("SELECT 1 FROM optouts WHERE phone=?", (phone,)).fetchone():
        return "opted out"
    if db.execute("SELECT 1 FROM reactivation WHERE phone=?", (phone,)).fetchone():
        return "already sent"
    return None


def sent_last_24h() -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return _db().execute("SELECT COUNT(*) FROM reactivation WHERE sent_at>? AND lead_id NOT LIKE 'test%'",
                         (since,)).fetchone()[0]


def status() -> dict:
    db = _db()
    return {"running": _state["running"], "log": _state["log"][-200:], "sent_24h": sent_last_24h(),
            "daily_limit": DAILY_LIMIT,
            "total_sent": db.execute("SELECT COUNT(*) FROM reactivation WHERE lead_id NOT LIKE 'test%'").fetchone()[0],
            "optouts": db.execute("SELECT COUNT(*) FROM optouts").fetchone()[0], "template": TEMPLATE}


# ---------- Chatwoot ----------
def _api(method: str, path: str, account_id: str, headers: dict, **kw):
    url = f"{os.getenv('CHATWOOT_URL', 'https://app.chatwoot.com').rstrip('/')}/api/v1/accounts/{account_id}/{path}"
    return httpx.request(method, url, headers=headers, timeout=30, **kw)


def _contact_id(phone: str, name: str, account_id: str, headers: dict) -> int:
    e164 = "+" + phone
    r = _api("GET", "contacts/search", account_id, headers, params={"q": e164})
    if r.status_code < 300:
        for c in r.json().get("payload", []):
            if (c.get("phone_number") or "") == e164:
                return c["id"]
    r = _api("POST", "contacts", account_id, headers,
             json={"inbox_id": WA_INBOX_ID, "name": name if name != "there" else e164, "phone_number": e164})
    if r.status_code >= 300:
        raise RuntimeError(f"contact {r.status_code}: {r.text[:200]}")
    body = r.json().get("payload", r.json())
    return (body.get("contact") or body)["id"]


def _send_one(row: dict, account_id: str, headers: dict) -> str:
    phone, name = row["phone"], (row.get("name") or "there").strip() or "there"
    price = str(row["price"]).replace("R", "").strip()
    params = {"1": name, "2": row["part_text"], "3": price}
    cid = _contact_id(phone, name, account_id, headers)
    payload = {
        "inbox_id": WA_INBOX_ID, "contact_id": cid, "source_id": phone, "status": "pending",
        "message": {"content": BODY.format(name=name, part=row["part_text"], price=price),
                    "template_params": {"name": TEMPLATE, "category": "MARKETING", "language": TEMPLATE_LANG,
                                        "processed_params": params}},
    }
    r = _api("POST", "conversations", account_id, headers, json=payload)
    if r.status_code >= 300:
        raise RuntimeError(f"conversation {r.status_code}: {r.text[:200]}")
    conv_id = str(r.json().get("id", ""))
    if conv_id:
        _api("POST", f"conversations/{conv_id}/labels", account_id, headers, json={"labels": ["old-lead-winback"]})
    return conv_id


def context_for(row: dict) -> str:
    return (f"OLD LEAD WIN-BACK. Some time ago this customer asked us: \"{row.get('request', '')}\". "
            f"We have just sent them a WhatsApp template saying we now have a {row['part_text']} that may fit, "
            f"from R{row['price']} (our match: {row.get('product', '')}, variant_id {row.get('variant_id', '')}). "
            "Fit is NOT confirmed yet: before any checkout link, confirm the year/model (or VIN) and side, "
            "check with search_stock / get_product, and offer the right item. If they reply STOP or don't want "
            "messages, reply once that they won't be contacted again and stop.")


def _run(rows: list[dict], account_id: str, token: str, test_phone: str = ""):
    headers = {"api_access_token": token, "Accept": "application/json",
               "User-Agent": "eAZyparts-lead-agent/1.0"}
    db = _db()
    try:
        for row in rows:
            phone = test_phone or row["phone"]
            tag = f"TEST to {phone[-4:]}" if test_phone else f"lead {row.get('lead_id')} …{phone[-4:]}"
            if not test_phone:
                why = already_contacted(phone)
                if why:
                    _state["log"].append(f"skip {tag}: {why}")
                    continue
                if sent_last_24h() >= DAILY_LIMIT:
                    _state["log"].append(f"stopped: daily limit of {DAILY_LIMIT} reached - send the rest tomorrow")
                    break
            try:
                conv_id = _send_one({**row, "phone": phone}, account_id, headers)
                db.execute("REPLACE INTO reactivation VALUES (?,?,?,?,?,?)",
                           (phone, ("test-" if test_phone else "") + str(row.get("lead_id", "")),
                            datetime.now(timezone.utc).isoformat(), conv_id, row.get("product", ""), str(row["price"])))
                db.execute("REPLACE INTO lead_forms VALUES (?,?)", (phone, context_for(row)))
                db.commit()
                _state["log"].append(f"sent {tag} (conversation {conv_id})")
            except Exception as e:  # keep going; show the reason on the page
                log.warning("reactivation %s failed: %s", tag, e)
                _state["log"].append(f"FAILED {tag}: {e}")
            time.sleep(GAP_SECONDS)
    finally:
        _state["running"] = False
        _state["log"].append("done")


def start(rows: list[dict], account_id: str, token: str, test_phone: str = "") -> str:
    with _lock:
        if _state["running"]:
            return "A send is already running."
        _state["running"] = True
        _state["log"].append(f"--- {datetime.now().strftime('%Y-%m-%d %H:%M')} starting {len(rows)} ---")
    threading.Thread(target=_run, args=(rows, account_id, token, test_phone), daemon=True).start()
    return "started"
