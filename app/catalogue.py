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
CACHE_SECONDS = int(os.getenv("CATALOGUE_CACHE_SECONDS", "7200"))
YEAR_WINDOW = 3
KEEP_FIELDS = ("id", "title", "handle", "product_type", "vendor", "tags", "variants", "images")
PAGE_PAUSE = float(os.getenv("CATALOGUE_PAGE_PAUSE", "2"))
CACHE_FILE = Path(os.getenv("CATALOGUE_CACHE_FILE", Path(os.getenv("AGENT_DB", "data/x")).parent / "catalogue_cache.json"))

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
    out = set()
    for w in re.findall(r"[a-z0-9\-]+", apply_synonyms(text)):
        out.add(w)
        if "-" in w:  # "c-class" also matches a customer typing "c class"
            out.update(x for x in w.split("-") if x)
    return {w for w in out if w not in STOPWORDS}


# Words that turn a part into a smaller accessory of it ("headlight bracket", "mirror glass").
# A listing with one of these only ranks high if the customer asked for it.
ACCESSORY_WORDS = {"bracket", "brackets", "ballast", "unit", "module", "control", "bulb", "cover", "cap",
                   "clip", "clips", "mount", "mounting", "bolt", "trim", "moulding", "molding", "washer",
                   "switch", "relay", "harness", "wiring", "motor", "glass", "lens", "seal", "gasket",
                   "support", "holder", "sensor", "adjuster", "tab", "repair", "kit", "cable", "hinge"}
# Chassis codes used in listing titles: Mercedes W-codes (W204, W205) and BMW E/F/G codes (E90, F30, G20).
# Kept narrow on purpose so model names like C200 or 320i are never mistaken for a chassis.
CHASSIS_RE = re.compile(r"^(w)(\d{3})$|^([efg])(\d{2})$")


def chassis_tokens(text: str) -> set[str]:
    return {w for w in words(text) if CHASSIS_RE.match(w) and not re.fullmatch(r"(19|20)\d\d", w)}


def chassis_conflict(title: str, chassis: set[str]) -> bool:
    """True if the listing names a different chassis code of the same family (e.g. W204 when we want W205)."""
    if not chassis:
        return False
    listed = chassis_tokens(title)
    if not listed or listed & chassis:
        return False
    family = lambda c: "bmw" if c[0] in "efg" else c[0]  # BMW E/F/G codes are successive generations
    fam = {family(c) for c in chassis}
    return any(family(c) in fam for c in listed)


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
        for attempt in range(8):
            r = c.get(f"{STORE_URL}/products.json", params={"limit": 250, "page": page})
            if r.status_code in (429, 430, 503):  # Shopify throttling: wait as long as it asks, then retry
                wait = float(r.headers.get("Retry-After") or 0) or min(60, 5 * 2 ** attempt)
                self.status["last_error"] = f"page {page}: HTTP {r.status_code}, waiting {wait:.0f}s"
                log.info("Shopify throttled page %s (HTTP %s), waiting %ss", page, r.status_code, wait)
                time.sleep(wait)
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
                out.extend({k: x.get(k) for k in KEEP_FIELDS} for x in batch)  # drop descriptions etc. to save memory
                self.status["pages"] = page
                if len(batch) < 250 or page >= 100:
                    break
                page += 1
                time.sleep(PAGE_PAUSE)  # be gentle with the store
        return out

    def _set(self, raw: list[dict], loaded_at: float) -> None:
        self._products = [p for p in (Product.from_shopify(x) for x in raw) if p and p.available]
        self._loaded_at = loaded_at

    def _load_disk_copy(self) -> bool:
        try:
            data = json.loads(CACHE_FILE.read_text())
            self._set(data["products"], data["saved_at"])
            self.status.update(state="loaded (saved copy)", products=len(self._products))
            log.info("catalogue: using saved copy with %s sellable products", len(self._products))
            return True
        except (OSError, ValueError, KeyError):
            return False

    def refresh(self) -> None:
        """Download the whole catalogue from Shopify (slow: ~20 pages). Safe to call from a background thread."""
        if self.source != "live":
            self._set(json.loads(SAMPLE_FILE.read_text())["products"], time.time())
            self.status.update(state="loaded", products=len(self._products))
            return
        if not self._lock.acquire(blocking=False):
            return  # a refresh is already running
        try:
            t0 = time.time()
            self.status.update(state="loading" if not self._products else "refreshing", pages=0)
            raw = self._fetch_live()
            self._set(raw, time.time())
            self.status.update(state="loaded", last_error=None, load_seconds=round(time.time() - t0, 1),
                               products=len(self._products), raw_products=len(raw))
            log.info("catalogue loaded: %s sellable of %s products in %ss", len(self._products), len(raw),
                     self.status["load_seconds"])
            try:
                CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                CACHE_FILE.write_text(json.dumps({"saved_at": self._loaded_at, "products": raw}))
            except OSError as e:
                log.warning("could not save catalogue copy: %s", e)
        except (httpx.HTTPError, ValueError) as e:
            self.status.update(state="error" if not self._products else "loaded (refresh failed)",
                               last_error=f"{type(e).__name__}: {e}"[:300])
            log.warning("catalogue load failed: %s", e)
        finally:
            self._lock.release()

    def start_background_refresh(self) -> None:
        """Use the saved copy straight away (if any), then keep the catalogue fresh in the background."""
        def loop():
            if self.source == "live" and self._load_disk_copy() and time.time() - self._loaded_at < CACHE_SECONDS:
                time.sleep(CACHE_SECONDS - (time.time() - self._loaded_at))
            while True:
                self.refresh()
                time.sleep(CACHE_SECONDS if self._products else 120)
        threading.Thread(target=loop, daemon=True, name="catalogue-refresh").start()

    def products(self) -> list[Product]:
        if not self._products and not self._lock.locked():
            self.refresh()  # first use without a background loader (tests, local runs)
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
               year: int | None = None, part_number: str = "", vin: str = "", chassis: str = "",
               limit: int = 3) -> dict:
        out = self._search(make, model, part, side, year, part_number, vin, chassis, limit)
        if not out["found"] and side and not out.get("catalogue_unavailable"):
            other = self._search(make, model, part, "", year, "", "", chassis, limit)
            if other["found"]:
                out["other_side_or_position"] = other["results"]
                out["note"] = ("Nothing for the requested side/position, but these fit the same vehicle on another "
                               "side/position. Only mention them if useful; never present them as the side asked for.")
        return out

    def _search(self, make, model, part, side, year, part_number, vin, chassis, limit) -> dict:
        prods = self.products()
        if not prods:
            return {"found": False, "results": [], "catalogue_unavailable": True,
                    "note": "Stock list is still loading. Do NOT say the part is out of stock. Keep collecting "
                            "vehicle/part details and search again in a moment, or hand over if it keeps failing."}

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
        want_chassis = chassis_tokens(f"{chassis} {model}")
        model_w = words(model) - want_chassis  # chassis is checked separately (listings don't always name it)
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
            if not side_ok(p) or not year_ok(p) or chassis_conflict(p.title, want_chassis):
                continue
            part_hits = len(part_w & p.text_words)
            if part_w and part_hits == 0:
                continue
            coverage = part_hits / max(len(part_w), 1)
            extras = (words(p.title) & ACCESSORY_WORDS) - part_w
            score = (coverage * 10 + (1 if year and year in p.years else 0)
                     + (2 if want_chassis & words(p.title) else 0) - 6 * bool(extras))
            scored.append((score, coverage, p, bool(extras)))
        scored.sort(key=lambda t: (-t[0], t[2].price))

        def summ(score, coverage, p):
            conf = "medium" if coverage >= 0.99 and score >= 9 else "low"
            note = "text match" + ("" if not year or year in p.years else f", year not exact (listed {p.years or 'no year'}), confirm fitment")
            return p.summary(note, conf)

        main = [summ(sc, cov, p) for sc, cov, p, acc in scored if not acc][:limit]
        related = [summ(sc, cov, p) for sc, cov, p, acc in scored if acc][:limit]
        out = {"found": bool(main), "results": main}
        if related and not main:
            out["related_items_only"] = related
            out["note"] = ("We don't have the part itself, only related items (brackets, units, covers...). "
                           "Treat the part as NOT in stock; mention these only if they could help.")
        return out


def checkout_link(variant_id: int, quantity: int = 1, lead_id: str | None = None) -> str:
    """Shopify cart permalink: goes straight to checkout, where shipping or collection is chosen."""
    url = f"{STORE_URL}/cart/{int(variant_id)}:{int(quantity)}"
    if lead_id:
        url += f"?attributes[lead_id]={lead_id}&ref=wa-agent"
    return url
