import base64, hashlib, hmac, json, tempfile, time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app import followups
from app.tracking import Tracker


def make_lead(t, lid="cw-101", phone="27821234567", variant=555, mins_ago=60):
    t.lead_started(lid, "WhatsApp", "Thabo Mokoena", phone, "1")
    t.customer_message(lid, "polo vivo side panel")
    t.checkout_sent(lid, "Polo Vivo L side panel", "https://eazyparts.co.za/cart/555:1", variant)
    t.agent_message(lid, "here's your link")
    # pretend the chat happened a while ago
    past = time.time() - mins_ago * 60
    t.db.execute("UPDATE leads SET last_customer_at=?, last_agent_at=? WHERE id=?", (past - 60, past - 30, lid))
    t.db.commit()
    return lid


def checkout(token, total, shipping, lead_id=None, phone=None, variant=555):
    return {"token": token, "total_price": str(total), "shipping_lines": [{"price": str(shipping)}],
            "note_attributes": [{"name": "lead_id", "value": lead_id}] if lead_id else [],
            "phone": phone, "line_items": [{"variant_id": variant}], "completed_at": None}


def agent_for(t):
    texts = []
    return SimpleNamespace(tracker=t, record_agent_text=lambda lid, txt: texts.append(txt)), texts


def backdate_checkout(t, lid, mins):
    t.db.execute("UPDATE leads SET checkout_last_at=? WHERE id=?", (time.time() - mins * 60, lid))
    t.db.commit()


def test_payment_rescue_escalates_and_replaces_nudge(monkeypatch):
    monkeypatch.setattr(followups, "sending_hours", lambda now=None: True)
    t = Tracker(Path(tempfile.mktemp(suffix=".db")))
    lid = make_lead(t, mins_ago=180)
    assert t.checkout_activity(checkout("tokA", 3100, 650, lead_id=lid)) == lid
    assert t.checkout_activity(checkout("tokB", 3100, 650, phone="082 123 4567")) == lid  # matched by phone
    assert t.checkout_activity(checkout("tokB", 3100, 650, lead_id=lid)) == lid  # same token again: not a new try
    assert len(json.loads(t.get(lid)["checkout_tokens"])) == 2
    # rescue not due yet (checkout 0 min ago), and the generic 2h nudge must NOT fire while rescue is pending
    assert t.due_for_rescue(45) == [] and t.due_for_nudge(2, 20) == []
    backdate_checkout(t, lid, 50)
    a, texts = agent_for(t)
    sent, escalated = [], []
    n = followups.run_once(a, lambda lead, txt: sent.append(txt) or True, lambda lead, note: escalated.append(note))
    assert n == 1 and "EFT" in sent[0] and "PayJustNow" in sent[0]
    assert escalated and "payment" in escalated[0]
    row = t.get(lid)
    assert row["rescue_sent"] == 1 and row["rescue_kind"] == "payment" and row["handed_over"] == 1
    assert followups.run_once(a, lambda *_: True) == 0  # never twice


def test_shipping_shock_and_general():
    t = Tracker(Path(tempfile.mktemp(suffix=".db")))
    lid = make_lead(t)
    t.checkout_activity(checkout("t1", 9387, 6256, lead_id=lid))
    lead = dict(t.get(lid))
    assert followups.rescue_kind(lead) == "shipping"
    assert "R6,256" in followups.compose_rescue(lead, "shipping") and "collect" in followups.compose_rescue(lead, "shipping")
    lid2 = make_lead(t, "cw-102", "27831112222", 777)
    t.checkout_activity(checkout("t2", 1200, 150, lead_id=lid2, variant=777))
    assert followups.rescue_kind(dict(t.get(lid2))) == "general"


def test_rescue_skips_paid_customer_replied_and_completed():
    t = Tracker(Path(tempfile.mktemp(suffix=".db")))
    lid = make_lead(t)
    assert t.checkout_activity({**checkout("t1", 900, 100, lead_id=lid), "completed_at": "2026-10-09"}) is None
    t.checkout_activity(checkout("t1", 900, 100, lead_id=lid))
    backdate_checkout(t, lid, 60)
    assert [r["id"] for r in t.due_for_rescue(45)] == [lid]
    t.customer_message(lid, "sorry, will pay tonight")  # customer spoke after checkout: agent handles it
    assert t.due_for_rescue(45) == []
    lid2 = make_lead(t, "cw-103", "27841112222", 888)
    t.checkout_activity(checkout("t9", 900, 100, lead_id=lid2, variant=888))
    backdate_checkout(t, lid2, 60)
    t.order_paid({"name": "#1600", "total_price": "900", "note_attributes": [{"name": "lead_id", "value": lid2}]})
    assert t.due_for_rescue(45) == []


def test_burst_cap(monkeypatch):
    monkeypatch.setattr(followups, "sending_hours", lambda now=None: True)
    monkeypatch.setattr(followups, "MAX_PER_RUN", 5)
    t = Tracker(Path(tempfile.mktemp(suffix=".db")))
    for i in range(12):
        make_lead(t, f"cw-{200+i}", f"2782000{i:04d}", 1000 + i, mins_ago=200)
    a, _ = agent_for(t)
    assert followups.run_once(a, lambda *_: True) == 5
    assert followups.run_once(a, lambda *_: True) == 5
    assert followups.run_once(a, lambda *_: True) == 2


def test_checkout_webhook_hmac(monkeypatch):
    from fastapi.testclient import TestClient
    import app.server as srv
    t = Tracker(Path(tempfile.mktemp(suffix=".db")))
    lid = make_lead(t)
    monkeypatch.setattr(srv, "SHOPIFY_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(srv, "agent", lambda: SimpleNamespace(tracker=t))
    c = TestClient(srv.app)
    body = json.dumps(checkout("tokZ", 2000, 300, lead_id=lid)).encode()
    assert c.post("/webhooks/shopify-checkouts", content=body, headers={"X-Shopify-Hmac-Sha256": "bad"}).status_code == 401
    sig = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    r = c.post("/webhooks/shopify-checkouts", content=body, headers={"X-Shopify-Hmac-Sha256": sig})
    assert r.status_code == 200 and r.json()["lead"] == lid
