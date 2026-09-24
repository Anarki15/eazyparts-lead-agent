"""Stock search over the eAZyparts Shopify catalogue.

Reads the live store's public product feed (or a local sample file for testing)
and ranks products against what the customer described.

Matching order, strongest first:
  1. OEM part number found in the product tags         -> "exact"
  2. First 11 characters of the VIN match a donor tag   -> "vin"
  3. Make + model + part words (+ side, year window)    -> "text"
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("eazyparts.catalogue")
STORE_URL = os.getenv("SHOPIFY_STORE_URL", "https://www.eazyparts.co.za").rstrip("/")
SAMPLE_FILE = Path(__file__).resolve().parent.parent / "data" / "sample_products.json"
CACHE_SECONDS = int(os.getenv("CATALOGUE_CACHE_SECONDS", "1800"))
YEAR_WINDOW = 3

# Words customers use -> words in eAZyparts titles/tags
SYNONYMS = {
    "headlamp": "headlight", "head light": "headlight", "head lamp": "headlight",
    "taillight": "tail light", "tail lamp": "tail light", "taillamp": "tail light",
    "hood": "bonnet", "wing": "fender", "mudguard": "fender", "grill": "grille",
    "side mirror": "mirror", "door mirror": "mirror", "wing mirror": "mirror",
    "windshield": "windscreen", "indicator": "indicator light", "loom": "wiring harness",
    "volkswagen": "vw", "merc": "mercedes-benz", "mercedes": "mercedes-benz", "benz": "mercedes-benz",
    "chev": "chevrolet", "chevy": "chevrolet", "passenger side": "left", "driver side": "right",
    "lhs": "left", "rhs": "right", "lh": "left", "rh": "right",
}
MAKE_ALIASES = {"vw": "volkswagen", "mercedes-benz": "mercedes-benz", "chevrolet": "chevrolet"}
SIDES = {"left", "right"}
ENDS = {"front", "rear"}
STOPWORDS = {"the", "a", "for", "my", "of", "and", "original", "new", "used", "oem", "part", "parts"}


def norm_code(s: str) -> str:
    """Normalise a part number / VIN fragment: uppercase, letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def apply_synonyms(text: str) -> str:
    t = f" {text.lower()} "
    for k in sorted(SYNONYMS, key=len, reverse=True):
        t = re.sub(rf"(?<![a-z]){re.escape(k)}(?![a-z])", SYNONYMS[k], t)
    return t.strip()


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9\-]+", apply_synonyms(text)) if w not in STOPWORDS}


@dataclass
class Product:
    id: int
    title: str
    handle: str
    product_type: str
    tags: list[str]
    variant_id: int
    price: float
    available: bool
    image: str | None = None
    # derived
    condition: str = ""
    origin: str = ""
    grade: str = ""
    damaged: bool = False
    years: list[int] = field(default_factory=list)
    codes: set[str] = field(default_factory=set)
    text_words: set[str] = field(default_factory=set)

    @classmethod
    def from_shopify(cls, p: dict) -> "Product | None":
        variants = p.get("variants") or []
        if not variants:
            return None
        v = variants[0]
        tags = p.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        images = p.get("images") or []
        prod = cls(
            id=p["id"], title=p["title"], handle=p.get("handle", ""),
            product_type=p.get("product_type", ""), tags=tags,
            variant_id=v["id"], price=float(v.get("price") or 0),
            available=bool(v.get("available", True)),
            image=images[0]["src"] if images else None,
        )
        tl = [t.lower() for t in tags]
        title_l = prod.title.lower()
        prod.condition = "New" if ("new" in tl or " new " in f" {title_l} ") else "Used"
        prod.origin = "Aftermarket" if ("aftermarket" in title_l or "not oem" in tl) else "OEM"
        prod.grade = next((t for t in tags if t in ("Gold", "Silver", "Bronze")), "")
        prod.damaged = "damaged" in tl
        prod.years = [int(t) for t in tags if re.fullmatch(r"(19|20)\d\d", t.strip())]
        prod.codes = {norm_code(t) for t in tags if len(norm_code(t)) >= 6 and re.search(r"\d", t)}
        prod.text_words = words(" ".join([prod.title, prod.product_type, " ".join(tags)]))
        return prod

    @property
    def url(self) -> str:
        return f"{STORE_URL}/products/{self.handle}"

    def summary(self, reason: str, confidence: str) -> dict:
        return {
            "title": self.title,
            "price_zar": round(self.price, 2),
            "condition": self.condition + (" (damaged, see photos)" if self.damaged else ""),
            "origin": self.origin,
            "grade": self.grade,
            "product_url": self.url,
            "image": self.image,
            "variant_id": self.variant_id,
            "match": reason,
            "confidence": confidence,
        }


class Catalogue:
    def __init__(self, source: str | None = None):
        # source: "live" (read the Shopify store) or "sample" (21 test products)
        self.source = source or os.getenv("CATALOGUE_SOURCE", "sample")
        self._products: list[Product] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()
        self.status = {"state": "not loaded", "pages": 0, "last_error": None, "load_seconds": None}

    # ---------- loading ----------
    def _get_page(self, c: httpx.Client, page: int) -> list[dict]:
        for attempt in range(4):
            r = c.get(f"{STORE_URL}/products.json", params={"limit": 250, "page": page})
            if r.status_code in (429, 430, 503):  # Shopify throttling: back off and retry
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("products", [])
        r.raise_for_status()
        return []

    def _fetch_live(self) -> list[dict]:
        out, page = [], 1
        with httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; eazyparts-lead-agent/1.0)"}) as c:
            while True:
                batch = self._get_page(c, page)
                out.extend(batch)
                self.status["pages"] = page
                if len(batch) < 250 or page >= 100:
                    break
                page += 1
        return out

    def products(self) -> list[Product]:
        if self._products and time.time() - self._loaded_at < CACHE_SECONDS:
            return self._products
        with self._lock:  # only one load at a time; others wait and reuse it
            if self._products and time.time() - self._loaded_at < CACHE_SECONDS:
                return self._products
            t0 = time.time()
            self.status["state"] = "loading"
            try:
                raw = self._fetch_live() if self.source == "live" else json.loads(SAMPLE_FILE.read_text())["products"]
            except (httpx.HTTPError, ValueError) as e:
                self.status.update(state="error", last_error=f"{type(e).__name__}: {e}"[:300])
                log.warning("catalogue load failed: %s", e)
                if self._products:  # store unreachable: keep using the last good copy
                    return self._products
                raise
            self._products = [p for p in (Product.from_shopify(x) for x in raw) if p and p.available]
            self._loaded_at = time.time()
            self.status.update(state="loaded", last_error=None, load_seconds=round(time.time() - t0, 1),
                               products=len(self._products), raw_products=len(raw))
            log.info("catalogue loaded: %s sellable of %s products in %ss", len(self._products), len(raw),
                     self.status["load_seconds"])
            return self._products

    def get(self, variant_id: int) -> Product | None:
        return next((p for p in self.products() if p.variant_id == int(variant_id)), None)

    def still_available(self, p: Product) -> bool:
        """Re-check one product live before sending a checkout link (used parts are usually 1 in stock)."""
        if self.source != "live":
            return p.available
        try:
            r = httpx.get(f"{STORE_URL}/products/{p.handle}.js", timeout=10, follow_redirects=True,
                          headers={"User-Agent": "eazyparts-lead-agent/1.0"})
            if r.status_code == 404:
                return False
            r.raise_for_status()
            return any(v.get("id") == p.variant_id and v.get("available") for v in r.json().get("variants", []))
        except httpx.HTTPError:
            return True  # can't check right now; Shopify checkout still blocks sold-out items

    # ---------- search ----------
    def search(self, make: str = "", model: str = "", part: str = "", side: str = "",
               year: int | None = None, part_number: str = "", vin: str = "", limit: int = 3) -> dict:
        prods = self.products()

        # 1. Part number: exact on normalised tags
        pn = norm_code(part_number)
        if len(pn) >= 6:
            hits = [p for p in prods if pn in p.codes]
            if hits:
                return {"found": True, "results": [p.summary("part number match", "high") for p in hits[:limit]]}

        q_side = words(side) | words(part)
        want_lr = q_side & SIDES
        want_fr = q_side & ENDS
        make_w = words(MAKE_ALIASES.get(apply_synonyms(make), make))
        model_w = words(model)
        part_w = words(part) - SIDES - ENDS

        def side_ok(p: Product) -> bool:
            tw = words(p.title)
            if want_lr and (tw & SIDES) and not (tw & want_lr):
                return False
            if want_fr and (tw & ENDS) and not (tw & want_fr):
                return False
            return True

        def year_ok(p: Product) -> bool:
            if not year or not p.years:
                return True
            return any(abs(y - int(year)) <= YEAR_WINDOW for y in p.years)

        # 2. VIN: first 11 chars vs donor-vehicle tag
        v = norm_code(vin)
        if len(v) >= 11:
            vin_hits = [p for p in prods if v[:11] in p.codes and side_ok(p)]
            if part_w:
                vin_hits = [p for p in vin_hits if part_w & p.text_words]
            if vin_hits:
                return {"found": True, "results": [p.summary("VIN match (same vehicle type)", "high") for p in vin_hits[:limit]]}

        # 3. Text match: make and model must both hit, then score part words
        scored = []
        for p in prods:
            if make_w and not (make_w & p.text_words):
                continue
            if model_w and not model_w <= p.text_words:
                continue
            if not side_ok(p) or not year_ok(p):
                continue
            part_hits = len(part_w & p.text_words)
            if part_w and part_hits == 0:
                continue
            coverage = part_hits / max(len(part_w), 1)
            score = coverage * 10 + (1 if year and year in p.years else 0)
            scored.append((score, coverage, p))
        scored.sort(key=lambda t: (-t[0], t[2].price))
        results = []
        for score, coverage, p in scored[:limit]:
            conf = "medium" if coverage >= 0.99 else "low"
            note = "text match" + ("" if not year or year in p.years else f", year not exact (listed {p.years or 'no year'}), confirm fitment")
            results.append(p.summary(note, conf))
        return {"found": bool(results), "results": results}


def checkout_link(variant_id: int, quantity: int = 1, lead_id: str | None = None) -> str:
    """Shopify cart permalink: goes straight to checkout, where shipping or collection is chosen."""
    url = f"{STORE_URL}/cart/{int(variant_id)}:{int(quantity)}"
    if lead_id:
        url += f"?attributes[lead_id]={lead_id}&ref=wa-agent"
    return url
