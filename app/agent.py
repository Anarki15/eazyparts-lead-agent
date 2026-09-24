"""The agent brain: one Claude tool-use loop per customer message."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .catalogue import Catalogue, checkout_link
from .leads import format_card, save_lead
from .prompt import build_system_prompt
from .tracking import Tracker

MODEL = os.getenv("AGENT_MODEL", "claude-haiku-4-5")
MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 40  # messages kept per conversation
DB_PATH = Path(os.getenv("AGENT_DB", Path(__file__).resolve().parent.parent / "data" / "conversations.db"))
PHOTO_DIR = DB_PATH.parent / "photos"
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

TOOLS = [
    {
        "name": "search_stock",
        "description": "Search live eAZyparts Shopify stock. Use part_number or vin when known; otherwise make + model + part (+ side, year).",
        "input_schema": {
            "type": "object",
            "properties": {
                "make": {"type": "string", "description": "e.g. Toyota"},
                "model": {"type": "string", "description": "e.g. Hilux"},
                "year": {"type": "integer"},
                "part": {"type": "string", "description": "e.g. headlight, front bumper, left door mirror"},
                "side": {"type": "string", "description": "left/right and/or front/rear, if relevant"},
                "part_number": {"type": "string"},
                "vin": {"type": "string"},
            },
        },
    },
    {
        "name": "get_product",
        "description": "Get full details of one product by variant_id (from search_stock results).",
        "input_schema": {"type": "object", "properties": {"variant_id": {"type": "integer"}}, "required": ["variant_id"]},
    },
    {
        "name": "make_checkout_link",
        "description": "Create a checkout link for the product the customer chose. Shipping is chosen at checkout.",
        "input_schema": {
            "type": "object",
            "properties": {"variant_id": {"type": "integer"}, "quantity": {"type": "integer", "default": 1}},
            "required": ["variant_id"],
        },
    },
    {
        "name": "save_lead_card",
        "description": "Save/update the lead card for staff and the Google Sheets tracker. Call before hand_over, and when a checkout link is sent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"}, "make": {"type": "string"}, "model": {"type": "string"},
                "year": {"type": "string"}, "vin": {"type": "string"}, "part": {"type": "string"},
                "side": {"type": "string"}, "part_number": {"type": "string"},
                "photos": {"type": "string", "description": "What the customer's photos show (disc, part, etc.)"},
                "preference": {"type": "string", "enum": ["OEM only", "Alternative or used is fine", "Show me both", "Not asked yet"]},
                "delivery_town": {"type": "string"},
                "stock_result": {"type": "string", "description": "What you searched and the closest matches"},
                "outcome": {"type": "string", "enum": ["checkout link sent", "sourcing request", "handover", "in progress"]},
            },
        },
    },
    {
        "name": "hand_over",
        "description": "Pass the chat to a human. The agent then stays silent in this conversation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "priority": {"type": "string", "enum": ["normal", "high", "urgent"]},
            },
            "required": ["reason", "priority"],
        },
    },
]


@dataclass
class Conversation:
    id: str
    channel: str = "test"
    customer_name: str = ""
    phone: str = ""
    campaign: str = ""
    messages: list = field(default_factory=list)
    lead: dict = field(default_factory=dict)
    handed_over: bool = False
    context: str = ""  # e.g. what the customer filled in on a Meta lead form


class Store:
    """Tiny SQLite store for conversation state."""

    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS conv (id TEXT PRIMARY KEY, data TEXT)")

    def load(self, conv_id: str) -> Conversation | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM conv WHERE id=?", (conv_id,)).fetchone()
        return Conversation(**json.loads(row[0])) if row else None

    def save(self, c: Conversation) -> None:
        with self.lock:
            self.db.execute("REPLACE INTO conv VALUES (?,?)", (c.id, json.dumps(c.__dict__)))
            self.db.commit()


@dataclass
class TurnResult:
    replies: list[str]
    handed_over: bool = False
    handover_reason: str = ""
    priority: str = ""
    lead_note: str = ""


class Agent:
    def __init__(self, client=None, catalogue: Catalogue | None = None, store: Store | None = None,
                 tracker: Tracker | None = None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.catalogue = catalogue or Catalogue()
        self.store = store or Store()
        self.tracker = tracker or Tracker(self.store_path().parent / "tracking.db")

    def store_path(self) -> Path:
        return DB_PATH

    def save_photo(self, conv_id: str, data_b64: str, media_type: str) -> str:
        """Keep a copy of every customer photo so staff can see it on the dashboard. Returns the file name."""
        folder = PHOTO_DIR / conv_id
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time() * 1000)}.{IMAGE_TYPES.get(media_type, 'jpg')}"
        (folder / name).write_bytes(base64.b64decode(data_b64))
        return name

    # ---------- tools ----------
    def _run_tool(self, conv: Conversation, name: str, args: dict, result: TurnResult) -> dict:
        if name == "search_stock":
            args = {k: v for k, v in args.items() if v not in (None, "")}
            out = self.catalogue.search(**args)
            conv.lead["stock_result"] = (
                "; ".join(r["title"] for r in out["results"]) if out["found"] else f"no match for {args}"
            )
            if not out.get("catalogue_unavailable"):
                self.tracker.stock_search(conv.id, args, out["found"], conv.lead["stock_result"])
            return out
        if name == "get_product":
            p = self.catalogue.get(args["variant_id"])
            return p.summary("lookup", "high") | {"tags": p.tags} if p else {"error": "not found"}
        if name == "make_checkout_link":
            p = self.catalogue.get(args["variant_id"])
            if not p or not self.catalogue.still_available(p):
                return {"error": "That product has just sold out. Apologise, then offer to search again or start a sourcing request."}
            conv.lead["outcome"] = "checkout link sent"
            url = checkout_link(p.variant_id, args.get("quantity", 1), conv.id)
            self.tracker.checkout_sent(conv.id, p.title, url, p.variant_id)
            return {"checkout_url": url, "title": p.title,
                    "price_zar": p.price, "note": "Delivery calculated at checkout, or collect in Bloemfontein."}
        if name == "save_lead_card":
            conv.lead.update({k: v for k, v in args.items() if v})
            return {"ok": True}
        if name == "hand_over":
            conv.handed_over = True
            result.handed_over = True
            result.handover_reason = args["reason"]
            result.priority = args["priority"]
            card = {**conv.lead, "lead_id": conv.id, "channel": conv.channel,
                    "customer_name": conv.lead.get("customer_name") or conv.customer_name,
                    "phone": conv.phone, "campaign": conv.campaign,
                    "handover_reason": args["reason"], "priority": args["priority"],
                    "outcome": conv.lead.get("outcome") or ("sourcing request" if "sourcing" in args["reason"] else "handover")}
            saved = save_lead(card)
            result.lead_note = format_card(saved["row"])
            self.tracker.handed_over(conv.id, args["reason"], args["priority"], card)
            return {"ok": True, "google_sheet": saved["google_sheet"]}
        return {"error": f"unknown tool {name}"}

    # ---------- main turn ----------
    def handle(self, conv_id: str, text: str = "", image_urls: list[str] | None = None,
               channel: str = "test", customer_name: str = "", phone: str = "", campaign: str = "",
               context: str = "", images: list[dict] | None = None, account_id: str = "") -> TurnResult:
        """images: [{"data": <base64>, "media_type": "image/jpeg"}] (photos the customer sent)."""
        conv = self.store.load(conv_id) or Conversation(id=conv_id, channel=channel, customer_name=customer_name,
                                                        phone=phone, campaign=campaign, context=context)
        self.tracker.lead_started(conv_id, channel, customer_name, phone, account_id)
        photo_names = [self.save_photo(conv_id, im["data"], im.get("media_type", "image/jpeg"))
                       for im in images or []]
        self.tracker.customer_message(conv_id, text, photo_names)
        if conv.handed_over:
            return TurnResult(replies=[], handed_over=True)  # staff are handling it

        content = []
        for url in image_urls or []:
            content.append({"type": "image", "source": {"type": "url", "url": url}})
        for im in images or []:
            content.append({"type": "image", "source": {"type": "base64", "media_type": im.get("media_type", "image/jpeg"),
                                                        "data": im["data"]}})
        content.append({"type": "text", "text": text or "(customer sent a photo)"})
        conv.messages.append({"role": "user", "content": content})

        result = TurnResult(replies=[])
        system = build_system_prompt()
        if conv.customer_name:
            system += f"\nCustomer name from the platform: {conv.customer_name}."
        if conv.context:
            system += f"\nAlready known about this lead (don't re-ask): {conv.context}"

        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(
                model=MODEL, max_tokens=800, tools=TOOLS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=trim_history(conv.messages),
            )
            blocks = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in resp.content]
            conv.messages.append({"role": "assistant", "content": blocks})
            texts = [b["text"] for b in blocks if b["type"] == "text" and b["text"].strip()]
            result.replies.extend(texts)
            for t in texts:
                self.tracker.agent_message(conv.id, t)
            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            if not tool_uses:
                break
            tool_results = []
            for tu in tool_uses:
                out = self._run_tool(conv, tu["name"], tu["input"] or {}, result)
                tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": json.dumps(out)})
            conv.messages.append({"role": "user", "content": tool_results})

        # Photos are big: once Claude has looked at them, keep only a note in the stored history.
        for m in conv.messages:
            if m["role"] == "user" and isinstance(m["content"], list):
                m["content"] = [c if c.get("type") != "image" or c["source"].get("type") != "base64"
                                else {"type": "text", "text": "[customer photo - already viewed above; details are in your replies/lead card]"}
                                for c in m["content"]]
        self.store.save(conv)
        return result


    def record_agent_text(self, conv_id: str, text: str) -> None:
        """Add a message sent outside a normal turn (e.g. a follow-up) to the chat history, so the agent knows it said it."""
        conv = self.store.load(conv_id)
        if not conv:
            return
        block = {"type": "text", "text": f"[Automatic follow-up sent] {text}"}
        if conv.messages and conv.messages[-1]["role"] == "assistant":
            conv.messages[-1]["content"].append(block)
        else:
            conv.messages.append({"role": "assistant", "content": [block]})
        self.store.save(conv)


def trim_history(messages: list) -> list:
    """Keep the last MAX_HISTORY messages, cutting only at a real customer message (never mid tool call)."""
    if len(messages) <= MAX_HISTORY:
        return messages
    start = len(messages) - MAX_HISTORY
    while start < len(messages):
        m = messages[start]
        if m["role"] == "user" and not any(c.get("type") == "tool_result" for c in m["content"]):
            return messages[start:]
        start += 1
    return messages[-1:]


def new_conversation_id() -> str:
    return uuid.uuid4().hex[:10]
