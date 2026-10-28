"""Regression tests for 28 Oct 2026: the 1 GB disk filled up, every database write failed,
the bot stopped answering customers and re-sent the same follow-up every 10 minutes."""
import os, sqlite3, tempfile, time
from pathlib import Path
from types import SimpleNamespace

from app import followups
from app.agent import Agent, Store, cleanup_photos
from app.catalogue import Catalogue
from app.tracking import Tracker


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(content=self.script.pop(0)))


def tb(text):
    return {"type": "text", "text": text}


def make_lead(t, lid, phone, variant, mins_ago=200):
    t.lead_started(lid, "WhatsApp", "Thabo", phone, "1")
    t.customer_message(lid, "polo vivo side panel")
    t.checkout_sent(lid, "Polo Vivo L side panel", f"https://eazyparts.co.za/cart/{variant}:1", variant)
    t.agent_message(lid, "here's your link")
    past = time.time() - mins_ago * 60
    t.db.execute("UPDATE leads SET last_customer_at=?, last_agent_at=?, checkout_last_at=NULL WHERE id=?",
                 (past - 60, past - 30, lid))
    t.db.commit()


def _disk_full(*_a, **_k):
    raise sqlite3.OperationalError("database or disk is full")


class FullTracker(Tracker):
    """A tracker whose every write fails like a full disk."""
    def _ensure(self, *a, **k):
        _disk_full()

    def event(self, *a, **k):
        _disk_full()


def test_customer_still_gets_reply_when_disk_full():
    fake = FakeClaude([[tb("Hi! Which vehicle is the part for?")]])
    store = Store(Path(tempfile.mktemp(suffix=".db")))
    store.save = _disk_full
    a = Agent(client=fake, catalogue=Catalogue("sample"), store=store,
              tracker=FullTracker(Path(tempfile.mktemp(suffix=".db"))))
    a.save_photo = lambda *a_, **k: _disk_full()
    r = a.handle("cw-900", "I'm looking for a Hilux rear bumper corner",
                 images=[{"data": "aGk=", "media_type": "image/jpeg"}])
    assert r.replies == ["Hi! Which vehicle is the part for?"]


def test_followups_never_repeat_when_recording_fails(monkeypatch):
    monkeypatch.setattr(followups, "sending_hours", lambda now=None: True)
    monkeypatch.setattr(followups, "MAX_PER_RUN", 15)
    followups._sent_at.clear()
    t = Tracker(Path(tempfile.mktemp(suffix=".db")))
    for i in range(20):
        make_lead(t, f"cw-{500+i}", f"2783000{i:04d}", 2000 + i, mins_ago=200)
    t.nudge_sent = _disk_full  # the disk fills up
    sent_to = []
    a = SimpleNamespace(tracker=t, record_agent_text=lambda *_: None)
    send = lambda lead, text: sent_to.append(lead["id"]) or True
    # 10 minutes later, and 10 minutes after that: nobody gets a second copy
    for _ in range(4):
        followups.run_once(a, send)
    assert len(sent_to) == len(set(sent_to)), "a customer got the same follow-up twice"
    assert len(sent_to) <= 4  # each run stops at the first message it can't record


def test_photo_cleanup_removes_only_old_photos():
    root = Path(tempfile.mkdtemp())
    old, new = root / "cw-1" / "old.jpg", root / "cw-2" / "new.jpg"
    for f in (old, new):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    assert cleanup_photos(30, root) == 1
    assert not old.exists() and new.exists()
    assert not (root / "cw-1").exists()  # empty folder tidied away
