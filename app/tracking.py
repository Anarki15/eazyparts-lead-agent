"""Lead tracking: one row per conversation with its furthest funnel stage, plus an event log.

Feeds the /dashboard page (drop-off funnel, 'needs a human' queue, 'gone quiet' list).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

# Funnel stages, in order. A lead's stage only ever moves forward.
STAGES = [
    ("started", "Lead arrived"),
    ("engaged", "Customer replied"),
    ("searched", "Vehicle + part known, stock searched"),
    ("found", "Part found in stock"),
    ("checkout", "Checkout link sent"),
]
STAGE_RANK = {s: i for i, (s, _) in enumerate(STAGES)}
STAFF_STATUSES = ["New", "Contacted", "Quoted", "Won", "Lost"]


class Tracker:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
              id TEXT PRIMARY KEY, channel TEXT, name TEXT, phone TEXT,
              created_at REAL, last_customer_at REAL, last_agent_at REAL, updated_at REAL,
              stage TEXT DEFAULT 'started', customer_msgs INTEGER DEFAULT 0,
              vehicle TEXT DEFAULT '', part TEXT DEFAULT '', stock_result TEXT DEFAULT '',
              sourcing INTEGER DEFAULT 0, handed_over INTEGER DEFAULT 0, handover_reason TEXT DEFAULT '',
              priority TEXT DEFAULT '', handed_over_at REAL, staff_status TEXT DEFAULT '', staff_note TEXT DEFAULT '',
              photos TEXT DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS events (lead_id TEXT, ts REAL, kind TEXT, detail TEXT);
            CREATE INDEX IF NOT EXISTS ev_lead ON events(lead_id);
            """)
            self.db.commit()

    # ---------- writes ----------
    def _ensure(self, lead_id: str, **fields):
        now = time.time()
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO leads (id, created_at, updated_at) VALUES (?,?,?)",
                            (lead_id, now, now))
            if fields:
                cols = ", ".join(f"{k}=?" for k in fields)
                self.db.execute(f"UPDATE leads SET {cols}, updated_at=? WHERE id=?", (*fields.values(), now, lead_id))
            self.db.commit()

    def event(self, lead_id: str, kind: str, detail: str | dict = ""):
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)",
                            (lead_id, time.time(), kind, detail if isinstance(detail, str) else json.dumps(detail)))
            self.db.commit()

    def advance(self, lead_id: str, stage: str):
        row = self.get(lead_id)
        if row and STAGE_RANK[stage] > STAGE_RANK.get(row["stage"], 0):
            self._ensure(lead_id, stage=stage)
            self.event(lead_id, "stage", stage)

    def lead_started(self, lead_id: str, channel: str, name: str = "", phone: str = ""):
        if not self.get(lead_id):
            self._ensure(lead_id, channel=channel, name=name, phone=phone)
            self.event(lead_id, "started", channel)

    def customer_message(self, lead_id: str, text: str, photos: list[str] | None = None):
        row = self.get(lead_id)
        n = (row["customer_msgs"] if row else 0) + 1
        fields = {"last_customer_at": time.time(), "customer_msgs": n}
        if photos:
            fields["photos"] = json.dumps(json.loads(row["photos"] or "[]") + photos)
        self._ensure(lead_id, **fields)
        self.event(lead_id, "customer", {"text": text[:500], "photos": photos or []})
        self.advance(lead_id, "engaged")

    def agent_message(self, lead_id: str, text: str):
        self._ensure(lead_id, last_agent_at=time.time())
        self.event(lead_id, "agent", text[:1000])

    def stock_search(self, lead_id: str, query: dict, found: bool, result: str):
        vehicle = " ".join(str(query.get(k, "")) for k in ("make", "model", "year")).strip()
        fields = {"stock_result": result[:300]}
        if vehicle:
            fields["vehicle"] = vehicle
        if query.get("part"):
            fields["part"] = " ".join(str(query.get(k, "")) for k in ("side", "part")).strip()
        self._ensure(lead_id, **fields)
        self.event(lead_id, "search", {"query": query, "found": found})
        self.advance(lead_id, "searched")
        if found:
            self.advance(lead_id, "found")

    def checkout_sent(self, lead_id: str, title: str, url: str):
        self.event(lead_id, "checkout", {"title": title, "url": url})
        self.advance(lead_id, "checkout")

    def handed_over(self, lead_id: str, reason: str, priority: str, card: dict):
        sourcing = 1 if "sourcing" in reason.lower() else 0
        vehicle = " ".join(str(card.get(k, "")) for k in ("make", "model", "year")).strip()
        fields = dict(handed_over=1, handover_reason=reason, priority=priority, handed_over_at=time.time(),
                      staff_status="New", sourcing=sourcing)
        if vehicle:
            fields["vehicle"] = vehicle
        if card.get("part"):
            fields["part"] = card["part"]
        self._ensure(lead_id, **fields)
        self.event(lead_id, "handover", {"reason": reason, "priority": priority})

    def set_staff_status(self, lead_id: str, status: str, note: str = ""):
        if status not in STAFF_STATUSES:
            raise ValueError(status)
        self._ensure(lead_id, staff_status=status, staff_note=note)
        self.event(lead_id, "staff", {"status": status, "note": note})

    # ---------- reads ----------
    def get(self, lead_id: str):
        with self.lock:
            return self.db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def events(self, lead_id: str):
        with self.lock:
            return self.db.execute("SELECT * FROM events WHERE lead_id=? ORDER BY ts", (lead_id,)).fetchall()

    def leads_since(self, since: float):
        with self.lock:
            return self.db.execute("SELECT * FROM leads WHERE created_at>=? ORDER BY created_at DESC",
                                   (since,)).fetchall()

    def summary(self, days: int = 7, quiet_hours: float = 2.0) -> dict:
        now = time.time()
        rows = self.leads_since(now - days * 86400)
        total = len(rows)
        reached = {s: 0 for s, _ in STAGES}
        for r in rows:
            rank = STAGE_RANK.get(r["stage"], 0)
            for s, i in STAGE_RANK.items():
                if rank >= i:
                    reached[s] += 1
        funnel = [{"stage": s, "label": label, "count": reached[s],
                   "pct": round(100 * reached[s] / total) if total else 0} for s, label in STAGES]
        sourcing = sum(r["sourcing"] for r in rows)
        needs_human = [dict(r) for r in rows if r["handed_over"] and r["staff_status"] in ("New", "")]
        prio = {"urgent": 0, "high": 1, "normal": 2}
        needs_human.sort(key=lambda r: (prio.get(r["priority"], 3), r["handed_over_at"] or 0))
        in_progress = [dict(r) for r in rows if r["handed_over"] and r["staff_status"] in ("Contacted", "Quoted")]
        gone_quiet = []
        for r in rows:
            if r["handed_over"] or r["stage"] == "started":
                continue
            last_c, last_a = r["last_customer_at"] or 0, r["last_agent_at"] or 0
            if last_a > last_c and now - last_c > quiet_hours * 3600:
                d = dict(r)
                d["quiet_hours"] = round((now - last_c) / 3600, 1)
                gone_quiet.append(d)
        gone_quiet.sort(key=lambda r: STAGE_RANK.get(r["stage"], 0), reverse=True)
        never_replied = [dict(r) for r in rows if r["stage"] == "started" and now - r["created_at"] > 3600]
        return {"days": days, "total": total, "funnel": funnel, "sourcing": sourcing,
                "handovers": sum(r["handed_over"] for r in rows), "needs_human": needs_human,
                "in_progress": in_progress, "gone_quiet": gone_quiet, "never_replied": never_replied,
                "won": sum(1 for r in rows if r["staff_status"] == "Won"),
                "lost": sum(1 for r in rows if r["staff_status"] == "Lost"),
                "recent": [dict(r) for r in rows[:50]]}
