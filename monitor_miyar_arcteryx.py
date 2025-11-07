# monitor_miyar_arcteryx.py
# -*- coding: utf-8 -*-
import json, os, re, time, math
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests

BASE = "https://store.miyaradventures.com/"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "snapshot.json")
USER_AGENT = "Mozilla/5.0 (compatible; ArcMonitor/1.0; +https://github.com)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

# ---------- Data models ----------
@dataclass
class VariantState:
    id: int
    title: str             # e.g., "Black / M"
    option1: Optional[str] # color
    option2: Optional[str] # size
    option3: Optional[str]
    sku: Optional[str]
    price: float
    compare_at_price: Optional[float]
    available: bool
    inventory_quantity: Optional[int]  # may be None if theme doesn’t expose

@dataclass
class ProductState:
    handle: str
    title: str
    vendor: Optional[str]
    url: str
    variants: Dict[str, VariantState]  # key by variant_id str

Snapshot = Dict[str, ProductState]  # key by handle

# ---------- Utilities ----------
def money_to_float(x) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, (int, float)):
            # Shopify often stores cents (e.g., 24900) on some endpoints; product.js uses decimal string
            # Normalize: if looks like cents integer and > 1000, divide by 100
            if isinstance(x, int) and x > 1000:
                return round(x / 100.0, 2)
            return float(x)
        s = str(x).strip().replace("$", "").replace(",", "")
        return round(float(s), 2)
    except Exception:
        return 0.0

def try_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if cur is None: return default
        cur = cur.get(k)
    return default if cur is None else cur

def get_json(url: str, retries: int = 3, timeout: int = 20):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                ct = r.headers.get("Content-Type", "")
                if "json" in ct or url.endswith(".js") or url.endswith(".json"):
                    return r.json()
                return None
            if r.status_code in (403, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None

def get_text(url: str, retries: int = 3, timeout: int = 20) -> Optional[str]:
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    return None

# ---------- Fetch product lists ----------
def fetch_products_via_products_json(limit: int = 250) -> List[dict]:
    """Try Shopify /products.json paginated."""
    out = []
    page = 1
    while True:
        url = urljoin(BASE, f"/products.json?limit={limit}&page={page}")
        data = get_json(url)
        if not data or "products" not in data or not data["products"]:
            break
        out.extend(data["products"])
        page += 1
        # polite delay
        time.sleep(0.6)
        # Avoid runaway
        if page > 40:
            break
    return out

def iter_sitemap_product_urls() -> List[str]:
    """Enumerate all product URLs via sitemap_products_*.xml."""
    urls = []
    idx = 1
    while True:
        url = urljoin(BASE, f"/sitemap_products_{idx}.xml")
        xml = get_text(url)
        if not xml:
            break
        try:
            root = ET.fromstring(xml)
            # Shopify sitemaps: <urlset><url><loc>...</loc></url>...
            for urlnode in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url"):
                loc = urlnode.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
                if loc is not None and loc.text:
                    urls.append(loc.text.strip())
        except ET.ParseError:
            break
        idx += 1
        if idx > 30:
            break
        time.sleep(0.4)
    return urls

def handle_from_product_url(purl: str) -> Optional[str]:
    # Expect /products/<handle>
    try:
        path = urlparse(purl).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "products":
            return parts[1]
    except Exception:
        pass
    return None

def fetch_product_js_by_handle(handle: str) -> Optional[dict]:
    url = urljoin(BASE, f"/products/{handle}.js")
    return get_json(url)

# ---------- Normalize to our snapshot schema ----------
def normalize_product(product_obj: dict, from_products_json: bool) -> Optional[ProductState]:
    # product.js has keys: title, vendor, handle, url, variants (with available, inventory_quantity possibly)
    if from_products_json:
        handle = product_obj.get("handle")
        title  = product_obj.get("title")
        vendor = product_obj.get("vendor")
        url    = urljoin(BASE, f"/products/{handle}")
        variants = {}
        for v in product_obj.get("variants", []):
            vid = v.get("id")
            variants[str(vid)] = VariantState(
                id = int(vid),
                title = v.get("title") or "",
                option1 = v.get("option1"),
                option2 = v.get("option2"),
                option3 = v.get("option3"),
                sku = v.get("sku"),
                price = money_to_float(v.get("price")),
                compare_at_price = money_to_float(v.get("compare_at_price")) if v.get("compare_at_price") else None,
                available = bool(v.get("available", False)),
                inventory_quantity = v.get("inventory_quantity") if isinstance(v.get("inventory_quantity"), int) else None
            )
        return ProductState(handle=handle, title=title, vendor=vendor, url=url, variants=variants)
    else:
        handle = product_obj.get("handle")
        title  = product_obj.get("title")
        vendor = product_obj.get("vendor")
        url    = product_obj.get("url") or urljoin(BASE, f"/products/{handle}")
        variants = {}
        for v in product_obj.get("variants", []):
            vid = v.get("id")
            variants[str(vid)] = VariantState(
                id = int(vid),
                title = v.get("title") or "",
                option1 = v.get("option1"),
                option2 = v.get("option2"),
                option3 = v.get("option3"),
                sku = v.get("sku"),
                price = money_to_float(v.get("price")),
                compare_at_price = money_to_float(v.get("compare_at_price")) if v.get("compare_at_price") else None,
                available = bool(v.get("available", False)),
                inventory_quantity = v.get("inventory_quantity") if isinstance(v.get("inventory_quantity"), int) else None
            )
        return ProductState(handle=handle, title=title, vendor=vendor, url=url, variants=variants)

def load_snapshot() -> Snapshot:
    if not os.path.exists(SNAPSHOT_PATH):
        return {}
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    snap: Snapshot = {}
    for handle, pdata in raw.items():
        variants = {
            vid: VariantState(**v) for vid, v in pdata["variants"].items()
        }
        snap[handle] = ProductState(
            handle=pdata["handle"], title=pdata["title"], vendor=pdata.get("vendor"),
            url=pdata["url"], variants=variants
        )
    return snap

def save_snapshot(snap: Snapshot):
    serializable = {
        h: {
            "handle": p.handle, "title": p.title, "vendor": p.vendor, "url": p.url,
            "variants": {vid: asdict(v) for vid, v in p.variants.items()}
        }
        for h, p in snap.items()
    }
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)

# ---------- Discord ----------
def send_discord(content: str):
    if not DISCORD_WEBHOOK:
        print("[WARN] DISCORD_WEBHOOK not set; printing instead:")
        print(content)
        return
    payload = {"content": content}
    # Avoid Origin/Referer to keep webhook happy
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    for i in range(3):
        try:
            r = SESSION.post(DISCORD_WEBHOOK, headers=headers, data=json.dumps(payload), timeout=20)
            if 200 <= r.status_code < 300:
                return
        except requests.RequestException:
            pass
        time.sleep(1.2 * (i + 1))

# ---------- Diff & reporting ----------
def variant_label(v: VariantState) -> str:
    parts = [p for p in [v.option1, v.option2, v.option3] if p]
    return " / ".join(parts) if parts else v.title or "Variant"

def report_new_product(p: ProductState):
    send_discord(f"🆕 上新 | {p.title} · {p.vendor or ''}\n{p.url}")

def report_new_variant(p: ProductState, v: VariantState):
    send_discord(f"🧩 新增变体 | {p.title} → {variant_label(v)}\n{p.url}")

def report_price_change(p: ProductState, old: VariantState, new: VariantState):
    send_discord(
        f"💲 价格变化 | {p.title} → {variant_label(new)}\n"
        f"{old.price:.2f} → {new.price:.2f}"
        + (f"（对比价 {new.compare_at_price:.2f}）" if new.compare_at_price else "") +
        f"\n{p.url}"
    )

def report_stock_status(p: ProductState, old: VariantState, new: VariantState):
    emoji = "✅" if new.available and not old.available else "⛔"
    send_discord(
        f"{emoji} 库存状态 | {p.title} → {variant_label(new)}\n"
        f"{'缺货→到货' if new.available and not old.available else '在售→缺货'}\n{p.url}"
    )

def report_inventory_increase(p: ProductState, old: VariantState, new: VariantState, delta: int):
    send_discord(
        f"📦 库存增加 | {p.title} → {variant_label(new)}\n"
        f"{old.inventory_quantity} → {new.inventory_quantity}（+{delta}）\n{p.url}"
    )

def is_arcteryx(product_title: str, vendor: Optional[str], tags: Optional[List[str]] = None) -> bool:
    v = (vendor or "").lower()
    t = (product_title or "").lower()
    if "arc'teryx" in v or "arcteryx" in v:
        return True
    if "arc'teryx" in t or "arcteryx" in t:
        return True
    if tags:
        low = [x.lower() for x in tags]
        if any(("arc'teryx" in x or "arcteryx" in x) for x in low):
            return True
    return False

def build_snapshot() -> Snapshot:
    snap: Snapshot = {}
    # Strategy A: products.json
    products = fetch_products_via_products_json()
    from_products_json = bool(products)
    handles: List[str] = []

    if from_products_json:
        for p in products:
            if not is_arcteryx(p.get("title", ""), p.get("vendor"), p.get("tags", [])):
                continue
            ps = normalize_product(p, from_products_json=True)
            if ps:
                snap[ps.handle] = ps
                handles.append(ps.handle)
        # Fallback enrich via .js (to get inventory_quantity if missing)
        for h in handles:
            time.sleep(0.25)
            jsobj = fetch_product_js_by_handle(h)
            if not jsobj:
                continue
            # only enrich inventory_quantity & available if provided
            jsnorm = normalize_product(jsobj, from_products_json=False)
            if not jsnorm:
                continue
            base = snap[h]
            for vid, v in base.variants.items():
                if vid in jsnorm.variants:
                    jsv = jsnorm.variants[vid]
                    if jsv.inventory_quantity is not None:
                        v.inventory_quantity = jsv.inventory_quantity
                    # some themes expose better available flag here
                    v.available = jsv.available
        return snap

    # Strategy B: sitemap + product.js
    prod_urls = iter_sitemap_product_urls()
    for url in prod_urls:
        handle = handle_from_product_url(url)
        if not handle:
            continue
        time.sleep(0.3)
        jsobj = fetch_product_js_by_handle(handle)
        if not jsobj:
            continue
        if not is_arcteryx(jsobj.get("title", ""), jsobj.get("vendor"), jsobj.get("tags", [])):
            continue
        ps = normalize_product(jsobj, from_products_json=False)
        if ps:
            snap[ps.handle] = ps
    return snap

def diff_and_report(old: Snapshot, new: Snapshot):
    # New product
    for handle, p in new.items():
        if handle not in old:
            report_new_product(p)

    # Existing products: check variants
    for handle, pnew in new.items():
        pold = old.get(handle)
        if not pold:
            continue
        # new variants
        for vid, vnew in pnew.variants.items():
            if vid not in pold.variants:
                report_new_variant(pnew, vnew)

        # changed variants
        for vid, vnew in pnew.variants.items():
            vold = pold.variants.get(vid)
            if not vold:
                continue
            # price change
            if abs(vnew.price - vold.price) > 1e-6:
                report_price_change(pnew, vold, vnew)
            # availability change
            if bool(vnew.available) != bool(vold.available):
                report_stock_status(pnew, vold, vnew)
            # inventory increase (only if both sides know numbers)
            if isinstance(vnew.inventory_quantity, int) and isinstance(vold.inventory_quantity, int):
                if vnew.inventory_quantity > vold.inventory_quantity:
                    report_inventory_increase(pnew, vold, vnew, vnew.inventory_quantity - vold.inventory_quantity)

def main():
    old = load_snapshot()
    new = build_snapshot()
    diff_and_report(old, new)
    save_snapshot(new)
    print(f"Done. Products tracked: {len(new)}")

if __name__ == "__main__":
    main()
