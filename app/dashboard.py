"""Lead dashboard: funnel / drop-off, 'needs a human' queue, 'gone quiet' list, and full transcripts."""
from __future__ import annotations

import html
import json
import time
from datetime import datetime

from .prompt import SAST
from .tracking import STAFF_STATUSES, Tracker

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#16181d;--muted:#667085;--line:#e4e7ec;--accent:#008069;--warn:#b54708;--bad:#b42318;--bar:#2e90fa}
@media (prefers-color-scheme:dark){:root{--bg:#101214;--card:#1a1d21;--ink:#e6e8eb;--muted:#98a2b3;--line:#2b3036;--accent:#3ccf9f;--warn:#f79009;--bad:#f97066;--bar:#53b1fd}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0}header nav a{margin-left:10px;color:var(--accent);text-decoration:none}
main{max-width:1200px;margin:0 auto;padding:16px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto}
.card h2{font-size:15px;margin:0 0 10px}.card h2 small{color:var(--muted);font-weight:400}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.tile b{display:block;font-size:24px}.tile span{color:var(--muted);font-size:12px}
.funnel .row{display:grid;grid-template-columns:230px 1fr 90px;gap:10px;align-items:center;margin:6px 0}
.funnel .bar{height:18px;background:var(--bar);border-radius:4px;min-width:2px}
.funnel .drop{color:var(--bad);font-size:12px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.urgent{color:var(--bad);border-color:var(--bad)}.high{color:var(--warn);border-color:var(--warn)}
a{color:var(--accent)}select,button,input{font:inherit;padding:4px 6px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
.muted{color:var(--muted)}.empty{color:var(--muted);padding:8px 0}
.chat{display:flex;flex-direction:column;gap:8px}.msg{max-width:80%;padding:8px 12px;border-radius:8px;white-space:pre-wrap}
.c{align-self:flex-start;background:var(--bg);border:1px solid var(--line)}.a{align-self:flex-end;background:#d9fdd3;color:#111}
.sys{align-self:center;font-size:12px;color:var(--muted)}.photos img{max-height:160px;border-radius:6px;margin:4px 4px 0 0}
"""


def _t(ts) -> str:
    return datetime.fromtimestamp(ts, SAST).strftime("%d %b %H:%M") if ts else ""


def _ago(ts) -> str:
    if not ts:
        return ""
    m = (time.time() - ts) / 60
    return f"{int(m)} min" if m < 60 else (f"{m / 60:.1f} h" if m < 48 * 60 else f"{m / 1440:.0f} d")


def _e(x) -> str:
    return html.escape(str(x or ""))


def _wa(phone: str) -> str:
    return f'<a href="https://wa.me/{_e(phone)}" target="_blank">{_e(phone)}</a>' if phone else '<span class="muted">test chat</span>'


def _page(title: str, key: str, body: str, refresh: bool = False) -> str:
    meta = '<meta http-equiv="refresh" content="60">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{meta}<title>{_e(title)}</title><style>{CSS}</style></head><body>
<header><h1>eAZyparts lead agent</h1><nav><a href="/dashboard?key={_e(key)}">Dashboard</a><a href="/test?key={_e(key)}">Test chat</a></nav></header>
<main>{body}</main></body></html>"""


def _status_form(lead_id: str, current: str, key: str) -> str:
    opts = "".join(f'<option {"selected" if s == current else ""}>{s}</option>' for s in STAFF_STATUSES)
    return (f'<form method="post" action="/dashboard/status" style="display:flex;gap:4px">'
            f'<input type="hidden" name="key" value="{_e(key)}"><input type="hidden" name="lead_id" value="{_e(lead_id)}">'
            f'<select name="status">{opts}</select><button>Save</button></form>')


def render_dashboard(t: Tracker, key: str, days: int = 7) -> str:
    s = t.summary(days=days)
    tiles = [
        (s["total"], "leads"),
        (f'{s["funnel"][1]["pct"]}%', "replied to the agent"),
        (s["funnel"][3]["count"], "found in stock"),
        (s["funnel"][4]["count"], "checkout links sent"),
        (s["sourcing"], "sourcing requests"),
        (len(s["needs_human"]), "waiting for a human"),
        (f'{s["won"]} / {s["lost"]}', "won / lost (staff)"),
    ]
    tiles_html = "".join(f'<div class="tile"><b>{v}</b><span>{_e(lbl)}</span></div>' for v, lbl in tiles)

    top = max(s["funnel"][0]["count"], 1)
    rows, prev = [], None
    for f in s["funnel"]:
        drop = f'<span class="drop">-{prev - f["count"]} dropped</span>' if prev is not None and prev > f["count"] else ""
        rows.append(f'<div class="row"><span>{_e(f["label"])}</span>'
                    f'<div><div class="bar" style="width:{100 * f["count"] / top:.0f}%"></div></div>'
                    f'<span><b>{f["count"]}</b> ({f["pct"]}%) {drop}</span></div>')
        prev = f["count"]
    funnel = "".join(rows)

    def lead_link(r):
        return f'<a href="/dashboard/lead/{_e(r["id"])}?key={_e(key)}">{_e(r["name"] or r["id"])}</a>'

    nh = "".join(
        f'<tr><td><span class="pill {_e(r["priority"])}">{_e(r["priority"] or "normal")}</span></td>'
        f'<td>{lead_link(r)}<br>{_wa(r["phone"])}</td><td>{_e(r["vehicle"])}<br><span class="muted">{_e(r["part"])}</span></td>'
        f'<td>{_e(r["handover_reason"])}</td><td>{_ago(r["handed_over_at"])}</td>'
        f'<td>{_status_form(r["id"], r["staff_status"] or "New", key)}</td></tr>'
        for r in s["needs_human"]) or '<tr><td colspan="6" class="empty">Nobody waiting.</td></tr>'

    ip = "".join(
        f'<tr><td>{lead_link(r)}<br>{_wa(r["phone"])}</td><td>{_e(r["vehicle"])} · {_e(r["part"])}</td>'
        f'<td>{_e(r["staff_note"])}</td><td>{_status_form(r["id"], r["staff_status"], key)}</td></tr>'
        for r in s["in_progress"]) or '<tr><td colspan="4" class="empty">None.</td></tr>'

    stage_label = {k: v for k, v in [(f["stage"], f["label"]) for f in s["funnel"]]}
    gq = "".join(
        f'<tr><td>{lead_link(r)}<br>{_wa(r["phone"])}</td><td>{_e(stage_label.get(r["stage"], r["stage"]))}</td>'
        f'<td>{_e(r["vehicle"])} · {_e(r["part"])}</td><td>{r["quiet_hours"]} h</td></tr>'
        for r in s["gone_quiet"]) or '<tr><td colspan="4" class="empty">No one has gone quiet.</td></tr>'

    recent = "".join(
        f'<tr><td>{_t(r["created_at"])}</td><td>{lead_link(r)}</td><td>{_e(r["channel"])}</td>'
        f'<td>{_e(stage_label.get(r["stage"], r["stage"]))}{" · handed over" if r["handed_over"] else ""}</td>'
        f'<td>{_e(r["vehicle"])} · {_e(r["part"])}</td></tr>'
        for r in s["recent"]) or '<tr><td colspan="5" class="empty">No leads yet.</td></tr>'

    ranges = " ".join(f'<a href="/dashboard?key={_e(key)}&days={d}">{"<b>" if d == days else ""}{d} days{"</b>" if d == days else ""}</a>'
                      for d in (1, 7, 30))
    body = f"""
<div class="muted">Showing the last {days} days · {ranges} · updates every minute</div>
<div class="tiles">{tiles_html}</div>
<section class="card"><h2>Needs a human <small>handed over by the agent, not yet contacted. WhatsApp them, then set the status.</small></h2>
<table><tr><th>Priority</th><th>Customer</th><th>Vehicle / part</th><th>Why</th><th>Waiting</th><th>Status</th></tr>{nh}</table></section>
<section class="card"><h2>Drop-off funnel <small>furthest step each lead reached</small></h2><div class="funnel">{funnel}</div></section>
<section class="card"><h2>Gone quiet <small>agent replied, customer hasn't answered for 2+ hours. Worth a personal nudge.</small></h2>
<table><tr><th>Customer</th><th>Stopped at</th><th>Vehicle / part</th><th>Quiet for</th></tr>{gq}</table></section>
<section class="card"><h2>Being worked on <small>contacted or quoted</small></h2>
<table><tr><th>Customer</th><th>Vehicle / part</th><th>Note</th><th>Status</th></tr>{ip}</table></section>
<section class="card"><h2>All recent leads</h2>
<table><tr><th>Arrived</th><th>Customer</th><th>Channel</th><th>Reached</th><th>Vehicle / part</th></tr>{recent}</table></section>
"""
    return _page("Lead dashboard", key, body, refresh=True)


def render_lead(t: Tracker, lead_id: str, key: str) -> str:
    r = t.get(lead_id)
    if not r:
        return _page("Not found", key, '<div class="card">Lead not found.</div>')
    chat = []
    for ev in t.events(lead_id):
        d = ev["detail"]
        if ev["kind"] == "customer":
            d = json.loads(d)
            pics = "".join(f'<img src="/media/{_e(lead_id)}/{_e(p)}?key={_e(key)}">' for p in d.get("photos", []))
            chat.append(f'<div class="msg c">{_e(d["text"]) or "<i>photo</i>"}<div class="photos">{pics}</div>'
                        f'<div class="muted">{_t(ev["ts"])}</div></div>')
        elif ev["kind"] == "agent":
            chat.append(f'<div class="msg a">{_e(d)}<div class="muted">{_t(ev["ts"])}</div></div>')
        else:
            chat.append(f'<div class="sys">{_t(ev["ts"])} · {_e(ev["kind"])}: {_e(d)}</div>')
    current = r["staff_status"] or "New"
    opts = "".join(f'<option {"selected" if st == current else ""}>{st}</option>' for st in STAFF_STATUSES)
    info = (f'<p><b>{_e(r["name"] or r["id"])}</b> · {_wa(r["phone"])} · {_e(r["channel"])} · arrived {_t(r["created_at"])}</p>'
            f'<p>Vehicle: {_e(r["vehicle"]) or "-"} · Part: {_e(r["part"]) or "-"}<br>Stock search: {_e(r["stock_result"]) or "-"}</p>'
            f'<p>Handed over: {"yes - " + _e(r["handover_reason"]) if r["handed_over"] else "no"}</p>'
            f'<form method="post" action="/dashboard/status" style="display:flex;gap:6px;flex-wrap:wrap">'
            f'<input type="hidden" name="key" value="{_e(key)}"><input type="hidden" name="lead_id" value="{_e(lead_id)}">'
            f'<select name="status">{opts}</select>'
            f'<input name="note" placeholder="Note (e.g. quoted R1,200)" value="{_e(r["staff_note"])}" style="flex:1;min-width:200px"><button>Save</button></form>')
    body = f'<section class="card">{info}</section><section class="card"><h2>Conversation</h2><div class="chat">{"".join(chat)}</div></section>'
    return _page(f"Lead {lead_id}", key, body)
