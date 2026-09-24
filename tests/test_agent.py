import json
import os
import tempfile
from datetime import datetime
from types import SimpleNamespace

os.environ["LEADS_CSV"] = tempfile.mktemp(suffix=".csv")
os.environ["AGENT_DB"] = tempfile.mktemp(suffix=".db")

from app.agent import Agent, Store, trim_history, MAX_HISTORY  # noqa: E402
from app.catalogue import Catalogue, checkout_link  # noqa: E402
from app.prompt import SAST, office_status  # noqa: E402

cat = Catalogue("sample")


# ---------- stock matching ----------
def test_part_number_exact_any_spelling():
    r = cat.search(part_number="2Q0-955-453G")
    assert r["found"] and "Polo 2020 Windscreen Washer" in r["results"][0]["title"]
    assert r["results"][0]["confidence"] == "high"


def test_vin_match():
    # Yaris donor tag VNKKG3D330A = first 11 of VIN
    r = cat.search(vin="VNKKG3D330A123456", part="wiring harness")
    assert r["found"] and "Yaris" in r["results"][0]["title"]


def test_text_with_side_and_synonym():
    r = cat.search(make="Isuzu", model="D-Max", part="headlamp", side="right", year=2018)
    assert r["found"] and "Right Headlight" in r["results"][0]["title"]
    assert "year not exact" in r["results"][0]["match"]


def test_wrong_side_excluded():
    r = cat.search(make="Isuzu", model="D-Max", part="headlight", side="left")
    assert not r["found"]


def test_year_outside_window_excluded():
    r = cat.search(make="Toyota", model="Yaris", part="wiring harness", year=2008)
    assert not r["found"]


def test_make_alias_vw():
    r = cat.search(make="Volkswagen", model="Polo", part="number plate light")
    assert r["found"] and "License Plate Light" in r["results"][0]["title"]


def test_not_in_stock():
    r = cat.search(make="Toyota", model="Hilux", part="bonnet")
    assert r == {"found": False, "results": []}


def test_checkout_link():
    assert checkout_link(123, 1, "abc").endswith("/cart/123:1?attributes[lead_id]=abc&ref=wa-agent")


def test_office_hours():
    assert office_status(datetime(2026, 9, 24, 10, 0, tzinfo=SAST))[0] is True
    closed, when = office_status(datetime(2026, 9, 25, 21, 0, tzinfo=SAST))  # Friday night
    assert closed is False and "Monday" in when


# ---------- agent loop with a scripted fake Claude ----------
class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        import copy; self.calls.append(copy.deepcopy(kw))
        return SimpleNamespace(content=self.script.pop(0))


def tb(text):
    return {"type": "text", "text": text}


def tu(i, name, inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def test_stock_found_flow():
    fake = FakeClaude([
        [tu("t1", "search_stock", {"make": "Isuzu", "model": "D-Max", "part": "headlight", "side": "right", "year": 2017})],
        [tb("Good news, we have 1 in stock: Isuzu Aftermarket D-Max 2017 Right Headlight, R1,022. Want it?")],
        [tu("t2", "make_checkout_link", {"variant_id": 49000000018})],
        [tb("Here's your checkout link. Delivery is calculated at checkout.")],
    ])
    a = Agent(client=fake, catalogue=cat, store=Store())
    r1 = a.handle("c1", "Right headlight 2017 D-Max please")
    assert "in stock" in r1.replies[0]
    # the tool result fed back to Claude contains the real product
    assert "Right Headlight" in fake.calls[1]["messages"][-1]["content"][0]["content"]
    r2 = a.handle("c1", "Yes")
    link_result = fake.calls[3]["messages"][-1]["content"][0]["content"]
    assert "/cart/49000000018:1" in link_result
    assert not r2.handed_over


def test_sourcing_handover_and_silence():
    fake = FakeClaude([
        [tu("s1", "search_stock", {"make": "Toyota", "model": "Hilux", "part": "bonnet"})],
        [tb("We don't have that on the shelf, but we can source it. Please send a photo of your licence disc.")],
        [tu("s2", "save_lead_card", {"make": "Toyota", "model": "Hilux", "part": "bonnet", "vin": "AHTFR22G106012345",
                                     "preference": "Show me both", "delivery_town": "Kimberley"}),
         tu("s3", "hand_over", {"reason": "sourcing request complete", "priority": "normal"})],
        [tb("Thanks! A parts specialist will WhatsApp you with a quote.")],
    ])
    a = Agent(client=fake, catalogue=cat, store=Store())
    a.handle("c2", "Hilux 2019 bonnet")
    r = a.handle("c2", "VIN AHTFR22G106012345, show me both, Kimberley")
    assert r.handed_over and "VIN: AHTFR22G106012345" in r.lead_note and "Preference: Show me both" in r.lead_note
    assert "no match" in r.lead_note  # stock search recorded on the card
    csv_text = open(os.environ["LEADS_CSV"]).read()
    assert "Kimberley" in csv_text and "sourcing request" in csv_text
    # after handover the agent stays silent
    n = len(fake.calls)
    r3 = a.handle("c2", "hello?")
    assert r3.replies == [] and len(fake.calls) == n


def test_trim_history_never_starts_on_tool_result():
    msgs = []
    for i in range(30):
        msgs += [{"role": "user", "content": [tb(f"q{i}")]},
                 {"role": "assistant", "content": [tu(f"x{i}", "search_stock", {})]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"x{i}", "content": "{}"}]}]
    out = trim_history(msgs)
    assert len(out) <= MAX_HISTORY and out[0]["content"][0]["type"] == "text"


# ---------- web endpoints ----------
def test_chatwoot_webhook_filters_and_meta_verify(monkeypatch):
    from fastapi.testclient import TestClient
    import app.server as srv
    seen = []
    monkeypatch.setattr(srv, "_process_chatwoot", lambda ev: seen.append(ev))
    monkeypatch.setattr(srv, "META_VERIFY_TOKEN", "tok")
    c = TestClient(srv.app)
    base = {"event": "message_created", "message_type": "incoming", "content": "hi",
            "conversation": {"id": 5, "status": "pending"}, "account": {"id": 1}}
    c.post("/webhooks/chatwoot", json=base)
    c.post("/webhooks/chatwoot", json={**base, "conversation": {"id": 5, "status": "open"}})  # human has it
    c.post("/webhooks/chatwoot", json={**base, "message_type": "outgoing"})
    assert len(seen) == 1
    r = c.get("/webhooks/meta-leads", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"})
    assert r.text == "42"
    assert c.get("/webhooks/meta-leads", params={"hub.mode": "subscribe", "hub.verify_token": "bad"}).status_code == 403


def test_sa_phone():
    from app.server import sa_phone
    assert sa_phone("082 123 4567") == "27821234567"
    assert sa_phone("+27 82 123 4567") == "27821234567"


# ---------- tracking + dashboard ----------
def test_tracking_funnel_and_dashboard(monkeypatch):
    import base64 as b64
    from fastapi.testclient import TestClient
    import app.server as srv
    fake = FakeClaude([
        [tu("d1", "search_stock", {"make": "Isuzu", "model": "D-Max", "part": "headlight", "side": "right"})],
        [tb("Found it: Right Headlight R1,022. Want it?")],
        [tu("d2", "make_checkout_link", {"variant_id": 49000000018})],
        [tb("Here is your link.")],
        [tb("Thanks for the photo, which vehicle is it for?")],
        [tu("d3", "hand_over", {"reason": "customer asked for a person", "priority": "high"})],
        [tb("Connecting you now.")],
    ])
    from pathlib import Path
    from app.tracking import Tracker
    a = Agent(client=fake, catalogue=cat, store=Store(), tracker=Tracker(Path(tempfile.mktemp(suffix=".db"))))
    monkeypatch.setattr(srv, "_agent", a)
    monkeypatch.setattr(srv, "TEST_PAGE_KEY", "k")
    a.handle("t1", "D-Max right headlight")
    a.handle("t1", "yes")
    png = b64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    a.handle("t2", "", images=[{"data": png, "media_type": "image/png"}])
    a.handle("t2", "just let me talk to someone")
    # photo not kept in history as base64
    assert "base64" not in json.dumps(a.store.load("t2").messages)
    s = a.tracker.summary()
    stages = {f["stage"]: f["count"] for f in s["funnel"]}
    assert stages["started"] == 2 and stages["engaged"] == 2 and stages["checkout"] == 1
    assert [r["id"] for r in s["needs_human"]] == ["t2"]
    c = TestClient(srv.app)
    assert "Open dashboard" in c.get("/dashboard").text
    page = c.get("/dashboard?key=k").text
    assert "Needs a human" in page and "customer asked for a person" in page
    lead = c.get("/dashboard/lead/t2?key=k").text
    photo = json.loads(a.tracker.get("t2")["photos"])[0]
    assert f"/media/t2/{photo}" in lead
    assert c.get(f"/media/t2/{photo}?key=k").status_code == 200
    assert c.get(f"/media/t2/{photo}?key=bad").status_code == 403
    r = c.post("/dashboard/status", data={"key": "k", "lead_id": "t2", "status": "Contacted", "note": "called"},
               follow_redirects=False)
    assert r.status_code == 303 and a.tracker.get("t2")["staff_status"] == "Contacted"
    assert a.tracker.summary()["needs_human"] == []
