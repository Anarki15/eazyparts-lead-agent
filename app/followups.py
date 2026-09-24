"""Automatic follow-ups for customers who go quiet.

Rules (all inside WhatsApp's free 24-hour reply window, so no paid templates are needed):
  - 1st nudge after 2 hours of silence, 2nd after 20 hours. Never more than 2.
  - Only between 07:30 and 20:00 SAST (nobody wants a parts reminder at 2am).
  - Never to chats handed to staff, customers who opted out (STOP), or leads that already paid.
  - If the customer replies, the count resets and the agent carries on the conversation.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime

from .prompt import SAST

log = logging.getLogger("eazyparts.followups")
ENABLED = os.getenv("FOLLOWUPS_ENABLED", "true").lower() == "true"
FIRST_AFTER_H = float(os.getenv("FOLLOWUP_FIRST_HOURS", "2"))
SECOND_AFTER_H = float(os.getenv("FOLLOWUP_SECOND_HOURS", "20"))
SEND_FROM, SEND_UNTIL = (7, 30), (20, 0)


def sending_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(SAST)
    return SEND_FROM <= (now.hour, now.minute) < SEND_UNTIL


def _last_checkout_url(tracker, lead_id: str) -> str:
    for ev in reversed(tracker.events(lead_id)):
        if ev["kind"] == "checkout":
            return json.loads(ev["detail"]).get("url", "")
    return ""


def compose(tracker, lead: dict) -> str:
    name = (lead.get("name") or "").split(" ")[0]
    hi = f"Hi {name}" if name else "Hi"
    part = lead.get("part") or "part"
    second = (lead.get("nudges") or 0) >= 1
    stage = lead.get("stage")
    if stage == "checkout":
        title = lead.get("checkout_title") or part
        url = _last_checkout_url(tracker, lead["id"])
        if second:
            return (f"{hi}, last reminder from eAZyparts: the {title} is still in stock for now, "
                    f"but it's a one-off used part. Checkout: {url}\n"
                    "Reply if you have any questions, or STOP if you'd rather we didn't follow up.")
        return (f"{hi}, just checking in. The {title} is still available and here's your checkout link again: {url}\n"
                "Delivery is calculated at checkout, or you can collect in Bloemfontein.")
    if stage == "found":
        if second:
            return (f"{hi}, the {part} options I sent are still in stock for now. Reply with the one you'd like and "
                    "I'll send a checkout link, or STOP if you'd rather we didn't follow up.")
        return f"{hi}, did you want to go ahead with the {part}? Reply with the option you'd like and I'll send the checkout link."
    # engaged / searched: still collecting details
    if second:
        return (f"{hi}, we can still help you find that {part}. Send the vehicle details or a photo of your licence disc "
                "whenever you're ready. Reply STOP if you'd rather we didn't follow up.")
    return (f"{hi}, still looking for that {part}? Send me the vehicle make, model and year "
            "(or a photo of your licence disc) and I'll check our stock right away.")


def run_once(agent, send) -> int:
    """send(lead: dict, text: str) -> bool. Returns how many follow-ups were sent."""
    if not ENABLED or not sending_hours():
        return 0
    sent = 0
    for lead in agent.tracker.due_for_nudge(FIRST_AFTER_H, SECOND_AFTER_H):
        text = compose(agent.tracker, lead)
        try:
            if send(lead, text):
                agent.tracker.nudge_sent(lead["id"], text)
                agent.record_agent_text(lead["id"], text)
                sent += 1
        except Exception as e:  # noqa: BLE001 - one failed send must not stop the rest
            log.warning("follow-up to %s failed: %s", lead["id"], e)
    if sent:
        log.info("sent %s follow-ups", sent)
    return sent


def start_loop(agent, send, every_seconds: int = 600):
    def loop():
        while True:
            try:
                run_once(agent, send)
            except Exception as e:  # noqa: BLE001
                log.warning("follow-up loop error: %s", e)
            time.sleep(every_seconds)
    threading.Thread(target=loop, daemon=True, name="followups").start()
