# -*- coding: utf-8 -*-
"""
监控 https://store.miyaradventures.com/ 上所有 Arc'teryx 商品：
- 上新（新商品/新变体）
- 价格变化（新增）
- 仅提醒“缺货→到货”
- 库存数量增加
推送格式（示例）：
🔔 上新提醒 miyar
• 名称：Atom Hoody Men's
• 货号：X000009556
• 颜色：Trail Magic
• 价格：CA$ 360
🧾 库存信息：XL:1

（右侧商品缩略图）
"""
import json, os, time, xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse
import requests

BASE = "https://store.miyaradventures.com/"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "snapshot.json")
USER_AGENT = "Mozilla/5.0 (compatible; MiyarArcMonitor/1.0; +https://github.com)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})


@dataclass
class VariantState:
    id: int
    title: str
    option1: Optional[str]
    option2: Optional[str]
    sku: Optional[str]
    price: float
    available: bool
    inventory_quantity: Optional[int]


@dataclass
class ProductState:
    handle: str
    title: str
    vendor: Optional[str]
    url: str
    image: Optional[str]
    variants: Dict[str, VariantState]


def get_json(url: str):
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def get_text(url: str):
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


def money_to_float(x) -> float:
    try:
        if isinstance(x, (int, float)):
            if isinstance(x, int) and x > 1000:
                return round(x / 100, 2)
            return float(x)
        s = str(x).replace("$", "").replace(",", "")
        return float(s)
    except Exception:
        return 0.0


def try_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int):
            cur = cur[k] if 0 <= k < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def fetch_products_via_products_json(limit=250) -> List[dict]:
    products = []
    page = 1
    while True:
        data = get_json(f"{BASE}/products.json?limit={limit}&page={page}")
        if not data or not data.get("products"):
            break
        products.extend(data["products"])
        page += 1
        time.sleep(0.5)
        if page > 40:
            break
    return products


def iter_sitemap_product_urls() -> List[str]:
    urls = []
    idx = 1
    while True:
        xml = get_text(f"{BASE}/sitemap_products_{idx}.xml")
        if not xml:
            break
        try:
            root = ET.fromstring(xml)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for n in root.findall("sm:url", ns):
                loc = n.find("sm:loc", ns)
                if loc is not None and loc.text:
                    urls.append(loc.text.strip())
        except ET.ParseError:
            break
        idx += 1
        time.sleep(0.2)
    return urls


def handle_from_url(url: str) -> Optional[str]:
    try:
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "products":
            return parts[1]
    except Exception:
        return None
    return None


def fetch_product_js(handle: str):
    return get_json(f"{BASE}/products/{handle}.js")


def normalize_product(p: dict) -> Optional[ProductState]:
    handle = p.get("handle")
    if not handle:
        return None
    url = urljoin(BASE, f"/products/{handle}")
    image = try_get(p, "images", 0, "src") or try_get(p, "images", 0)
    variants = {}
    for v in p.get("variants", []):
        vid = str(v.get("id"))
        variants[vid] = VariantState(
            id=int(v.get("id")),
            title=v.get("title") or "",
            option1=v.get("option1"),
            option2=v.get("option2"),
            sku=v.get("sku"),
            price=money_to_float(v.get("price")),
            available=bool(v.get("available", False)),
            inventory_quantity=v.get("inventory_quantity") if isinstance(v.get("inventory_quantity"), int) else None,
        )
    return ProductState(
        handle=handle, title=p.get("title") or "", vendor=p.get("vendor"),
        url=url, image=image, variants=variants
    )


def is_arcteryx(title: str, vendor: Optional[str], tags=None) -> bool:
    t, v = title.lower(), (vendor or "").lower()
    if "arcteryx" in t or "arc'teryx" in t or "arcteryx" in v:
        return True
    if tags and any("arcteryx" in str(tag).lower() for tag in tags):
        return True
    return False


def load_snapshot() -> Dict[str, ProductState]:
    if not os.path.exists(SNAPSHOT_PATH):
        return {}
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    snap = {}
    for handle, pdata in raw.items():
        variants = {vid: VariantState(**v) for vid, v in pdata["variants"].items()}
        snap[handle] = ProductState(
            handle=pdata["handle"], title=pdata["title"], vendor=pdata.get("vendor"),
            url=pdata["url"], image=pdata.get("image"), variants=variants
        )
    return snap


def save_snapshot(snap):
    serializable = {
        h: {
            "handle": p.handle, "title": p.title, "vendor": p.vendor, "url": p.url, "image": p.image,
            "variants": {vid: asdict(v) for vid, v in p.variants.items()}
        } for h, p in snap.items()
    }
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def send_embed(description: str, thumb: Optional[str]):
    if not DISCORD_WEBHOOK:
        print(description)
        return
    embed = {
        "title": "🔔 通知 miyar",
        "color": 0x2B65EC,
        "description": description.strip()
    }
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=15)
    except Exception as e:
        print("Discord error:", e)


def format_inventory(p: ProductState) -> str:
    counts = {}
    for v in p.variants.values():
        size = v.option2 or v.option1 or "N/A"
        qty = v.inventory_quantity if isinstance(v.inventory_quantity, int) else (1 if v.available else 0)
        counts[size] = counts.get(size, 0) + max(0, qty)
    return " | ".join(f"{k}:{v}" for k, v in counts.items()) or "无"


def desc_new(p: ProductState) -> str:
    anyv = next(iter(p.variants.values()))
    price_line = f"• 价格：CA$ {anyv.price:.0f}" if anyv.price == int(anyv.price) else f"• 价格：CA$ {anyv.price:.2f}"
    return (
        f"🔔 上新提醒 miyar\n"
        f"• 名称：{p.title}\n"
        f"• 货号：{anyv.sku or '未知'}\n"
        f"• 颜色：{anyv.option1 or '未知'}\n"
        f"{price_line}\n"
        f"🧾 库存信息：{format_inventory(p)}\n\n"
        "（右侧商品缩略图）"
    )


def desc_restock(p: ProductState, v: VariantState) -> str:
    price_line = f"• 价格：CA$ {v.price:.0f}" if v.price == int(v.price) else f"• 价格：CA$ {v.price:.2f}"
    return (
        f"🔔 补货提醒 miyar\n"
        f"• 名称：{p.title}\n"
        f"• 货号：{v.sku or '未知'}\n"
        f"• 颜色：{v.option1 or '未知'}\n"
        f"{price_line}\n"
        f"🧾 库存信息：{v.option2 or 'N/A'}:{v.inventory_quantity if isinstance(v.inventory_quantity, int) else (1 if v.available else 0)}\n\n"
        "（右侧商品缩略图）"
    )


def desc_price_change(p: ProductState, vold: VariantState, vnew: VariantState) -> str:
    # 价格行：CA$ old → CA$ new（保留两位小数以避免混乱）
    return (
        f"🔔 价格变化 miyar\n"
        f"• 名称：{p.title}\n"
        f"• 货号：{vnew.sku or '未知'}\n"
        f"• 颜色：{vnew.option1 or '未知'}\n"
        f"• 价格：CA$ {vold.price:.2f} → CA$ {vnew.price:.2f}\n"
        f"🧾 库存信息：{vnew.option2 or 'N/A'}:{vnew.inventory_quantity if isinstance(vnew.inventory_quantity, int) else (1 if vnew.available else 0)}\n\n"
        "（右侧商品缩略图）"
    )


def build_snapshot() -> Dict[str, ProductState]:
    snap = {}
    products = fetch_products_via_products_json()
    if products:
        for p in products:
            if not is_arcteryx(p.get("title", ""), p.get("vendor"), p.get("tags", [])):
                continue
            ps = normalize_product(p)
            js = fetch_product_js(ps.handle)
            if js:
                jsn = normalize_product(js)
                if jsn:
                    ps.image = jsn.image or ps.image
                    for vid, v in ps.variants.items():
                        if vid in jsn.variants:
                            jsv = jsn.variants[vid]
                            v.available = jsv.available
                            if isinstance(jsv.inventory_quantity, int):
                                v.inventory_quantity = jsv.inventory_quantity
            snap[ps.handle] = ps
        return snap
    # 回退：sitemap + product.js
    urls = iter_sitemap_product_urls()
    for u in urls:
        h = handle_from_url(u)
        if not h:
            continue
        js = fetch_product_js(h)
        if not js:
            continue
        if not is_arcteryx(js.get("title", ""), js.get("vendor"), js.get("tags", [])):
            continue
        ps = normalize_product(js)
        if ps:
            snap[ps.handle] = ps
    return snap


def diff_and_report(old, new):
    # 新商品
    for handle, p in new.items():
        if handle not in old:
            send_embed(desc_new(p), p.image)

    # 变体新增 / 价格变化 / 仅“缺货→到货” / 库存增加
    for handle, pnew in new.items():
        pold = old.get(handle)
        if not pold:
            continue

        # 新增变体→当做上新提醒
        for vid, vnew in pnew.variants.items():
            if vid not in pold.variants:
                send_embed(desc_new(pnew), pnew.image)

        for vid, vnew in pnew.variants.items():
            vold = pold.variants.get(vid)
            if not vold:
                continue

            # 价格变化
            if abs((vnew.price or 0) - (vold.price or 0)) > 1e-6:
                send_embed(desc_price_change(pnew, vold, vnew), pnew.image)

            # 仅提醒“缺货→到货”
            if (not bool(vold.available)) and bool(vnew.available):
                send_embed(desc_restock(pnew, vnew), pnew.image)

            # 库存数量增加（两边都有数量时比较）
            if isinstance(vnew.inventory_quantity, int) and isinstance(vold.inventory_quantity, int):
                if vnew.inventory_quantity > vold.inventory_quantity:
                    send_embed(desc_restock(pnew, vnew), pnew.image)


def main():
    old = load_snapshot()
    new = build_snapshot()
    diff_and_report(old, new)
    save_snapshot(new)
    print(f"Done. Tracked: {len(new)} products")


if __name__ == "__main__":
    main()
