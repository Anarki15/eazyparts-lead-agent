"""Automatic follow-ups for customers who go quiet.

Rules (all inside WhatsApp's free 24-hour reply window, so no paid templates are needed):
  - 1st nudge after 2 hours of silence, 2nd after 20 hours. Never more than 2.
  - Only between 07:30 and 20:00 SAST (nobody wants a parts reminder at 2am).
  - Never to chats handed to staff, customers who opted out (STOP), or leads that already paid.
  - If the customer replies, the count resets and the agent carries on the conversation.
  - At most MAX_PER_RUN messages per 10-minute run, so the 07:30 catch-up is spread out instead of one burst.

Checkout rescue (needs the Shopify "Checkout update" webhook):
  When a customer opened Shopify's checkout from our link but didn't pay, the generic nudge is replaced by ONE
  message aimed at the likely reason, ~45 minutes later:
  - payment: 2+ checkout attempts (card declined) -> offer EFT / PayJustNow / pay on collection, hand to staff.
  - shipping: courier >= R500 and >= 40% of the part price -> offer freight quote / collection, hand to staff.
  - general: anything else -> ask what went wrong; the agent keeps handling the chat.
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
RESCUE_ENABLED = os.getenv("CHECKOUT_RESCUE_ENABLED", "true").lower() == "true"
FIRST_AFTER_H = float(os.getenv("FOLLOWUP_FIRST_HOURS", "2"))
SECOND_AFTER_H = float(os.getenv("FOLLOWUP_SECOND_HOURS", "20"))
RESCUE_AFTER_MIN = float(os.getenv("RESCUE_AFTER_MINUTES", "45"))
MAX_PER_RUN = int(os.getenv("FOLLOWUP_MAX_PER_RUN", "15"))
SHIPPING_SHOCK_RAND = float(os.getenv("SHIPPING_SHOCK_RAND", "500"))
SHIPPING_SHOCK_SHARE = float(os.getenv("SHIPPING_SHOCK_SHARE", "0.4"))
PAYMENT_OPTIONS = os.getenv(
    "PAYMENT_OPTIONS",
    "pay by EFT (we'll send our banking details and hold the part for you), split it into 3 interest-free "
    "payments with PayJustNow at checkout, or pay when you collect in Bloemfontein")
SEND_FROM, SEND_UNTIL = (7, 30), (20, 0)


def sending_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(SAST)
    return SEND_FROM <= (now.hour, now.minute) < SEND_UNTIL


def _last_checkout_url(tracker, lead_id: str) -> str:
    for ev in reversed(tracker.events(lead_id)):
        if ev["kind"] == "checkout":
            return json.loads(ev["detail"]).get("url", "")
    return ""


def _hi(lead: dict) -> str:
    name = (lead.get("name") or "").split(" ")[0]
    return f"Hi {name}" if name else "Hi"


def compose(tracker, lead: dict) -> str:
    hi = _hi(lead)
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


def rescue_kind(lead: dict) -> str:
    attempts = len(json.loads(lead.get("checkout_tokens") or "[]"))
    shipping = float(lead.get("checkout_shipping") or 0)
    goods = float(lead.get("checkout_total") or 0) - shipping
    if attempts >= 2:
        return "payment"
    if shipping >= SHIPPING_SHOCK_RAND and goods > 0 and shipping >= SHIPPING_SHOCK_SHARE * goods:
        return "shipping"
    return "general"


def compose_rescue(lead: dict, kind: str) -> str:
    hi = _hi(lead)
    title = lead.get("checkout_title") or lead.get("part") or "part"
    if kind == "payment":
        return (f"{hi}, it looks like the payment for the {title} didn't go through. That happens with some bank cards. "
                f"Easy ways around it: {PAYMENT_OPTIONS}. Which suits you? Our team will help you finish it.")
    if kind == "shipping":
        ship = float(lead.get("checkout_shipping") or 0)
        return (f"{hi}, I noticed the courier cost for the {title} came to R{ship:,.0f}. Big parts are expensive by "
                "standard courier, so we can get you a freight quote that's usually much cheaper, or you're welcome to "
                "collect at our store in Bloemfontein. Which town and suburb must it go to?")
    return (f"{hi}, I see you started checkout for the {title} but it didn't go through. Did something go wrong? "
            "Reply here and I'll help, whether it's payment, delivery or checking it fits your car.")


def _staff_note(lead: dict, kind: str) -> str:
    attempts = len(json.loads(lead.get("checkout_tokens") or "[]"))
    why = {"payment": f"{attempts} checkout attempts, payment probably failed. Offer EFT / PayJustNow / collection.",
           "shipping": (f"Courier R{float(lead.get('checkout_shipping') or 0):,.0f} on a "
                        f"R{float(lead.get('checkout_total') or 0):,.0f} checkout. Get a freight quote or offer collection.")}[kind]
    return (f"CHECKOUT RESCUE ({kind}): {lead.get('checkout_title') or lead.get('part') or ''} "
            f"for {lead.get('vehicle') or 'vehicle not captured'}. {why} Customer was sent the message above.")


def run_rescues(agent, send, escalate=None, limit: int = MAX_PER_RUN) -> int:
    """escalate(lead, note) hands the chat to staff (private note, label, priority, open)."""
    if not (ENABLED and RESCUE_ENABLED) or not sending_hours():
        return 0
    sent = 0
    for lead in agent.tracker.due_for_rescue(RESCUE_AFTER_MIN)[:limit]:
        kind = rescue_kind(lead)
        text = compose_rescue(lead, kind)
        try:
            if not send(lead, text):
                continue
            agent.tracker.rescue_done(lead["id"], kind, text)
            agent.record_agent_text(lead["id"], text)
            sent += 1
            if kind in ("payment", "shipping") and escalate:
                escalate(lead, _staff_note(lead, kind))
                agent.tracker.handed_over(lead["id"], f"checkout rescue: {kind}", "high", {})
        except Exception as e:  # noqa: BLE001
            log.warning("checkout rescue to %s failed: %s", lead["id"], e)
    if sent:
        log.info("sent %s checkout rescues", sent)
    return sent


def run_once(agent, send, escalate=None) -> int:
    """send(lead: dict, text: str) -> bool. Returns how many messages were sent."""
    if not ENABLED or not sending_hours():
        return 0
    sent = run_rescues(agent, send, escalate)
    for lead in agent.tracker.due_for_nudge(FIRST_AFTER_H, SECOND_AFTER_H):
        if sent >= MAX_PER_RUN:
            break  # the rest go out on the next run, 10 minutes later
        text = compose(agent.tracker, lead)
        try:
            if send(lead, text):
                agent.tracker.nudge_sent(lead["id"], text)
                agent.record_agent_text(lead["id"], text)
                sent += 1
        except Exception as e:  # noqa: BLE001 - one failed send must not stop the rest
            log.warning("follow-up to %s failed: %s", lead["id"], e)
    if sent:
        log.info("sent %s follow-ups/rescues", sent)
    return sent


def start_loop(agent, send, escalate=None, every_seconds: int = 600):
    def loop():
        while True:
            try:
                run_once(agent, send, escalate)
            except Exception as e:  # noqa: BLE001
                log.warning("follow-up loop error: %s", e)
            time.sleep(every_seconds)
    threading.Thread(target=loop, daemon=True, name="followups").start()
