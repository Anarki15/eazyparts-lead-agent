"""Web service: Chatwoot agent-bot webhook, Meta lead-form webhook, and a test chat page."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from .agent import DB_PATH, PHOTO_DIR, Agent, new_conversation_id
from .dashboard import render_dashboard, render_lead

AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() == "true"  # kill switch

CHATWOOT_URL = os.getenv("CHATWOOT_URL", "https://app.chatwoot.com").rstrip("/")
CHATWOOT_BOT_TOKEN = os.getenv("CHATWOOT_BOT_TOKEN", "")

GRAPH = "https://graph.facebook.com/v21.0"
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
META_PAGE_TOKEN = os.getenv("META_PAGE_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_TOKEN = os.getenv("WA_TOKEN", "")
WA_OPENER_TEMPLATE = os.getenv("WA_OPENER_TEMPLATE", "eazyparts_lead_opener")
WA_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_LANG", "en")

TEST_PAGE_KEY = os.getenv("TEST_PAGE_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = FastAPI(title="eAZyparts lead agent")
_agent: Agent | None = None
_agent_lock = threading.Lock()


def agent() -> Agent:
    global _agent
    with _agent_lock:
        if _agent is None:
            _agent = Agent()
        return _agent


# ---------- lead-form memory (phone -> what they filled in) ----------
def _forms_db():
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.execute("CREATE TABLE IF NOT EXISTS lead_forms (phone TEXT PRIMARY KEY, data TEXT)")
    return db


def sa_phone(raw: str) -> str:
    """0821234567 / +27 82 123 4567 -> 27821234567"""
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("0") and len(d) == 10:
        d = "27" + d[1:]
    return d


@app.on_event("startup")
def warm_catalogue():
    """Load the Shopify catalogue in the background and keep it fresh, so customers never wait for it."""
    agent().catalogue.start_background_refresh()


@app.get("/health")
def health():
    cat = agent().catalogue
    return {"ok": True, "agent_enabled": AGENT_ENABLED, "catalogue_source": cat.source,
            "products_loaded": len(cat._products), "catalogue": cat.status}


# ---------- Chatwoot agent bot ----------
def _cw(path: str, payload: dict, account_id: int):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{account_id}/conversations/{path}"
    httpx.post(url, json=payload, headers={"api_access_token": CHATWOOT_BOT_TOKEN}, timeout=20)


def _download_image(url: str) -> dict | None:
    """Fetch a customer photo from the inbox and hand it to Claude as base64 (inbox links can be private)."""
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True, headers={"api_access_token": CHATWOOT_BOT_TOKEN})
        r.raise_for_status()
        mt = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if mt not in ("image/jpeg", "image/png", "image/webp", "image/gif") or len(r.content) > 5_000_000:
            return None
        return {"data": base64.b64encode(r.content).decode(), "media_type": mt}
    except httpx.HTTPError:
        return None


def _process_chatwoot(event: dict):
    conv = event["conversation"]
    account_id = event["account"]["id"]
    conv_id = conv["id"]
    sender = (conv.get("meta") or {}).get("sender") or event.get("sender") or {}
    phone = sa_phone(sender.get("phone_number", ""))
    channel = "Messenger" if "Facebook" in (conv.get("channel") or "") else "WhatsApp"

    if not AGENT_ENABLED:
        _cw(f"{conv_id}/toggle_status", {"status": "open"}, account_id)
        return

    context = ""
    if phone:
        row = _forms_db().execute("SELECT data FROM lead_forms WHERE phone=?", (phone,)).fetchone()
        if row:
            context = row[0]

    images = [_download_image(a["data_url"]) for a in event.get("attachments") or [] if a.get("file_type") == "image"]
    images = [im for im in images if im]
    res = agent().handle(
        f"cw-{conv_id}", text=event.get("content") or "", images=images, channel=channel,
        customer_name=sender.get("name", ""), phone=phone, context=context,
    )
    for text in res.replies:
        _cw(f"{conv_id}/messages", {"content": text, "message_type": "outgoing", "private": False}, account_id)
    if res.handed_over:
        if res.lead_note:
            _cw(f"{conv_id}/messages", {"content": res.lead_note, "message_type": "outgoing", "private": True}, account_id)
        label = "sourcing-request" if "sourcing" in res.handover_reason else "needs-human"
        _cw(f"{conv_id}/labels", {"labels": [label, "via-agent"]}, account_id)
        if res.priority in ("high", "urgent"):
            _cw(f"{conv_id}/toggle_priority", {"priority": res.priority}, account_id)
        _cw(f"{conv_id}/toggle_status", {"status": "open"}, account_id)


@app.post("/webhooks/chatwoot")
async def chatwoot_webhook(request: Request, bg: BackgroundTasks):
    event = await request.json()
    if (event.get("event") == "message_created" and event.get("message_type") == "incoming"
            and (event.get("conversation") or {}).get("status") == "pending"):
        bg.add_task(_process_chatwoot, event)
    return {"ok": True}


# ---------- Meta lead forms ----------
@app.get("/webhooks/meta-leads")
def meta_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == META_VERIFY_TOKEN and META_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403)


def _process_lead(leadgen_id: str, form_id: str = "", ad_name: str = ""):
    r = httpx.get(f"{GRAPH}/{leadgen_id}", params={"access_token": META_PAGE_TOKEN}, timeout=20)
    r.raise_for_status()
    fields = {f["name"]: (f.get("values") or [""])[0] for f in r.json().get("field_data", [])}
    phone = sa_phone(fields.get("phone_number") or fields.get("phone") or "")
    if not phone:
        return
    name = fields.get("full_name") or fields.get("first_name") or "there"
    known = "; ".join(f"{k}: {v}" for k, v in fields.items() if k not in ("phone_number", "email") and v)
    part = next((v for k, v in fields.items() if "part" in k.lower()), "a vehicle part")
    db = _forms_db()
    db.execute("REPLACE INTO lead_forms VALUES (?,?)", (phone, f"Meta lead form ({ad_name or form_id}): {known}"))
    db.commit()
    # Opener template must be approved in WhatsApp Manager: body with {{1}} = name, {{2}} = part
    httpx.post(
        f"{GRAPH}/{WA_PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        json={"messaging_product": "whatsapp", "to": phone, "type": "template",
              "template": {"name": WA_OPENER_TEMPLATE, "language": {"code": WA_TEMPLATE_LANG},
                           "components": [{"type": "body", "parameters": [
                               {"type": "text", "text": name}, {"type": "text", "text": part}]}]}},
        timeout=20,
    )


@app.post("/webhooks/meta-leads")
async def meta_leads(request: Request, bg: BackgroundTasks):
    body = await request.json()
    for entry in body.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {})
            if ch.get("field") == "leadgen" and v.get("leadgen_id"):
                bg.add_task(_process_lead, v["leadgen_id"], v.get("form_id", ""), v.get("ad_id", ""))
    return {"ok": True}


# ---------- Test chat page (no WhatsApp needed) ----------
def _check_key(key: str | None):
    if TEST_PAGE_KEY and key != TEST_PAGE_KEY:
        raise HTTPException(403, "Add ?key=... to the URL")


@app.post("/test/chat")
async def test_chat(request: Request):
    body = await request.json()
    _check_key(body.get("key"))
    conv_id = body.get("conv_id") or new_conversation_id()
    images = []
    if body.get("image"):  # data URL from the photo button: "data:image/jpeg;base64,...."
        head, _, data = body["image"].partition(",")
        images = [{"data": data, "media_type": head[5:].split(";")[0] or "image/jpeg"}]
    res = agent().handle(f"test-{conv_id}", text=body.get("text", ""), images=images, channel="Test page",
                         customer_name=body.get("name", ""))
    return {"conv_id": conv_id, "replies": res.replies, "handed_over": res.handed_over,
            "handover_reason": res.handover_reason, "lead_note": res.lead_note}


LOGIN_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>eAZyparts agent test</title><style>body{font:16px system-ui,sans-serif;background:#efeae2;display:grid;place-items:center;height:100vh;margin:0}
form{background:#fff;padding:24px;border-radius:12px;box-shadow:0 2px 8px #0002;display:flex;flex-direction:column;gap:12px;width:280px}
input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #ccc}button{background:#008069;color:#fff;border:0;cursor:pointer}
p{margin:0;color:#b00020;font-size:14px}</style></head><body><form method="get" action="/test">
<strong>eAZyparts agent test</strong>__MSG__<input name="key" type="password" placeholder="Test page password" autofocus required>
<button>Open test chat</button></form></body></html>"""


@app.get("/test", response_class=HTMLResponse)
def test_page(key: str | None = None):
    if TEST_PAGE_KEY and key != TEST_PAGE_KEY:
        msg = "<p>That password didn't match. Use the TEST_PAGE_KEY you set in Render.</p>" if key else ""
        return HTMLResponse(LOGIN_PAGE.replace("__MSG__", msg), status_code=200 if not key else 403)
    return (Path(__file__).parent / "test_page.html").read_text().replace("__KEY__", json.dumps(key or ""))


# ---------- Lead dashboard ----------
def _dash_key(key: str | None) -> str:
    want = os.getenv("DASHBOARD_KEY") or TEST_PAGE_KEY
    if want and key != want:
        raise HTTPException(403, "Open /test and enter the password first, or add ?key=... to the URL")
    return key or ""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(key: str | None = None, days: int = 7):
    if (os.getenv("DASHBOARD_KEY") or TEST_PAGE_KEY) and not key:
        return HTMLResponse(LOGIN_PAGE.replace('action="/test"', 'action="/dashboard"').replace("Open test chat", "Open dashboard").replace("__MSG__", ""))
    return render_dashboard(agent().tracker, _dash_key(key), days=max(1, min(days, 90)))


@app.get("/dashboard/lead/{lead_id}", response_class=HTMLResponse)
def dashboard_lead(lead_id: str, key: str | None = None):
    return render_lead(agent().tracker, lead_id, _dash_key(key))


@app.post("/dashboard/status")
async def dashboard_status(request: Request):
    form = await request.form()
    key = _dash_key(form.get("key"))
    agent().tracker.set_staff_status(form["lead_id"], form["status"], form.get("note", ""))
    back = request.headers.get("referer") or f"/dashboard?key={key}"
    return RedirectResponse(back, status_code=303)


@app.get("/media/{lead_id}/{name}")
def media(lead_id: str, name: str, key: str | None = None):
    _dash_key(key)
    path = (PHOTO_DIR / lead_id / name).resolve()
    if PHOTO_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)
