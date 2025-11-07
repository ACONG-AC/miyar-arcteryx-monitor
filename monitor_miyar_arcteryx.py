# -*- coding: utf-8 -*-
"""
调试版：强制 sitemap 模式 + 详细日志 + 确保 snapshot.json 写出
监控 https://store.miyaradventures.com/ 上所有 Arc'teryx 商品：
- 上新（新商品/新变体）
- 价格变化
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
import json, os, time, traceback, xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse
import requests

# ---------------- 基础配置 ----------------
BASE = "https://store.miyaradventures.com/"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "snapshot.json")
USER_AGENT = "Mozilla/5.0 (compatible; MiyarArcMonitor/1.0; +https://github.com)"
FORCE_SITEMAP_ONLY = True  # 调试版：强制只走 sitemap

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

def log(msg: str):
    print(f"[DEBUG] {msg}", flush=True)

# ---------------- 数据模型 ----------------
@dataclass
class VariantState:
    id: int
    title: str
    option1: Optional[str]
    option2: Optional[str]
    option3: Optional[str]
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

Snapshot = Dict[str, ProductState]

# ---------------- 工具函数 ----------------
def money_to_float(x) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, (int, float)):
            # 有些端点价格是分
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
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int):
            cur = cur[k] if 0 <= k < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur

def get_json(url: str, retries: int = 3, timeout: int = 20):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                ct = r.headers.get("Content-Type", "")
                if "json" in ct or url.endswith(".js") or url.endswith(".json"):
                    return r.json()
                else:
                    log(f"WARNING: Content-Type not json for {url}: {ct}")
                    # 尝试强行解析（若主题返回 js 对象文本会失败）
                    try:
                        return r.json()
                    except Exception:
                        return None
            else:
                log(f"get_json {url} -> HTTP {r.status_code}")
                if r.status_code in (403, 404):
                    return None
        except Exception as e:
            log(f"get_json exception {url}: {e}")
        time.sleep(1.0 * (i + 1))
    return None

def get_text(url: str, retries: int = 3, timeout: int = 20) -> Optional[str]:
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            else:
                log(f"get_text {url} -> HTTP {r.status_code}")
                if r.status_code in (403, 404):
                    return None
        except Exception as e:
            log(f"get_text exception {url}: {e}")
        time.sleep(1.0 * (i + 1))
    return None

# ---------------- 抓取（强制 sitemap） ----------------
def iter_sitemap_product_urls() -> List[str]:
    urls = []
    idx = 1
    while True:
        url = urljoin(BASE, f"/sitemap_products_{idx}.xml")
        xml = get_text(url)
        if not xml:
            log(f"sitemap_products_{idx}.xml not found, stop.")
            break
        try:
            root = ET.fromstring(xml)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0/9", "sm0": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            # 兼容两种命名
            nodes = root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}url")
            if not nodes:  # 兼容备用 ns key
                nodes = root.findall("sm0:url", {"sm0": "http://www.sitemaps.org/schemas/sitemap/0.9"})
            count = 0
            for n in nodes:
                loc = n.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
                if loc is None:
                    loc = n.find("sm0:loc", {"sm0":"http://www.sitemaps.org/schemas/sitemap/0.9"})
                if loc is not None and loc.text:
                    urls.append(loc.text.strip())
                    count += 1
            log(f"Parsed sitemap_products_{idx}.xml: {count} product URLs")
        except ET.ParseError as e:
            log(f"ParseError on sitemap_products_{idx}.xml: {e}")
            break
        idx += 1
        if idx > 50:
            log("Safety stop at 50 sitemaps.")
            break
        time.sleep(0.2)
    log(f"Total product URLs from sitemap: {len(urls)}")
    return urls

def handle_from_url(url: str) -> Optional[str]:
    try:
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "products":
            return parts[1]
    except Exception:
        pass
    return None

def fetch_product_js(handle: str) -> Optional[dict]:
    u = urljoin(BASE, f"/products/{handle}.js")
    js = get_json(u)
    if js is None:
        log(f"product.js fetch failed for handle={handle}")
    return js

# ---------------- 标准化 ----------------
def normalize_product_from_js(p: dict) -> Optional[ProductState]:
    handle = p.get("handle")
    if not handle:
        return None
    url = p.get("url") or urljoin(BASE, f"/products/{handle}")
    image = try_get(p, "images", 0)  # js 里 images 是 URL 数组
    variants: Dict[str, VariantState] = {}
    for v in p.get("variants", []):
        vid = str(v.get("id"))
        variants[vid] = VariantState(
            id=int(v.get("id")),
            title=v.get("title") or "",
            option1=v.get("option1"),
            option2=v.get("option2"),
            option3=v.get("option3"),
            sku=v.get("sku"),
            price=money_to_float(v.get("price")),
            available=bool(v.get("available", False)),
            inventory_quantity=v.get("inventory_quantity") if isinstance(v.get("inventory_quantity"), int) else None,
        )
    return ProductState(
        handle=handle,
        title=p.get("title") or "",
        vendor=p.get("vendor"),
        url=url,
        image=image,
        variants=variants,
    )

# ---------------- 识别品牌 ----------------
def is_arcteryx(title: str, vendor: Optional[str], tags=None) -> bool:
    t, v = (title or "").lower(), (vendor or "").lower()
    if "arcteryx" in t or "arc'teryx" in t:
        return True
    if "arcteryx" in v:
        return True
    if tags and any("arcteryx" in str(tag).lower() for tag in tags):
        return True
    return False

# ---------------- 快照 IO ----------------
def load_snapshot() -> Snapshot:
    if not os.path.exists(SNAPSHOT_PATH):
        log(f"snapshot not found at {os.path.abspath(SNAPSHOT_PATH)} (first run expected)")
        return {}
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        snap: Snapshot = {}
        for handle, pdata in raw.items():
            variants = {vid: VariantState(**v) for vid, v in pdata["variants"].items()}
            snap[handle] = ProductState(
                handle=pdata["handle"], title=pdata["title"], vendor=pdata.get("vendor"),
                url=pdata["url"], image=pdata.get("image"), variants=variants
            )
        log(f"snapshot loaded from {os.path.abspath(SNAPSHOT_PATH)} with {len(snap)} products")
        return snap
    except Exception as e:
        log(f"load_snapshot error: {e}\n{traceback.format_exc()}")
        return {}

def save_snapshot(snap: Snapshot):
    try:
        serializable = {
            h: {
                "handle": p.handle,
                "title": p.title,
                "vendor": p.vendor,
                "url": p.url,
                "image": p.image,
                "variants": {vid: asdict(v) for vid, v in p.variants.items()},
            }
            for h, p in snap.items()
        }
        abspath = os.path.abspath(SNAPSHOT_PATH)
        log(f"writing snapshot to {abspath}")
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        size = os.path.getsize(SNAPSHOT_PATH)
        log(f"snapshot written OK: {abspath} ({size} bytes)")
    except Exception as e:
        log(f"save_snapshot error: {e}\n{traceback.format_exc()}")

# ---------------- Discord（embed，miyar 文案） ----------------
def send_embed(description: str, thumb: Optional[str]):
    if not DISCORD_WEBHOOK:
        log("[NO WEBHOOK] printing message instead:\n" + description)
        return
    embed = {
        "title": "🔔 通知 miyar",
        "color": 0x2B65EC,
        "description": description.strip()
    }
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    try:
        r = SESSION.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20)
        if r.status_code >= 300:
            log(f"Discord HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Discord error: {e}")

def format_inventory(p: ProductState) -> str:
    counts: Dict[str, int] = {}
    for v in p.variants.values():
        size = v.option2 or v.option1 or "N/A"
        qty = v.inventory_quantity if isinstance(v.inventory_quantity, int) else (1 if v.available else 0)
        counts[size] = counts.get(size, 0) + max(0, int(qty))
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
    return (
        f"🔔 价格变化 miyar\n"
        f"• 名称：{p.title}\n"
        f"• 货号：{vnew.sku or '未知'}\n"
        f"• 颜色：{vnew.option1 or '未知'}\n"
        f"• 价格：CA$ {vold.price:.2f} → CA$ {vnew.price:.2f}\n"
        f"🧾 库存信息：{vnew.option2 or 'N/A'}:{vnew.inventory_quantity if isinstance(vnew.inventory_quantity, int) else (1 if vnew.available else 0)}\n\n"
        "（右侧商品缩略图）"
    )

# ---------------- 构建最新快照（强制 sitemap） ----------------
def build_snapshot() -> Snapshot:
    snap: Snapshot = {}
    log(f"FORCE_SITEMAP_ONLY = {FORCE_SITEMAP_ONLY}")
    urls = iter_sitemap_product_urls()
    if not urls:
        log("No product URLs from sitemap. Check robots/CF/CDN.")
        return {}

    handles = []
    for u in urls:
        h = handle_from_url(u)
        if h:
            handles.append(h)
    log(f"Handles extracted: {len(handles)}")

    ok, skipped = 0, 0
    for i, h in enumerate(handles, 1):
        if i % 25 == 0:
            log(f"Processing {i}/{len(handles)} ...")
        js = fetch_product_js(h)
        if not js:
            skipped += 1
            continue
        # 仅按 title/vendor 识别品牌
        if not is_arcteryx(js.get("title",""), js.get("vendor"), js.get("tags", [])):
            skipped += 1
            continue
        ps = normalize_product_from_js(js)
        if not ps or not ps.variants:
            skipped += 1
            continue
        snap[ps.handle] = ps
        ok += 1
        time.sleep(0.2)  # 礼貌延迟
    log(f"Normalized products: ok={ok}, skipped={skipped}, total={len(snap)}")
    return snap

# ---------------- Diff & 推送 ----------------
def diff_and_report(old: Snapshot, new: Snapshot):
    # 新商品
    for handle, p in new.items():
        if handle not in old:
            log(f"[NEW PRODUCT] {p.title} ({handle})")
            send_embed(desc_new(p), p.image)

    # 变体新增 / 价格变化 / 仅“缺货→到货” / 库存增加
    for handle, pnew in new.items():
        pold = old.get(handle)
        if not pold:
            continue

        # 新增变体 -> 当做上新提醒
        for vid, vnew in pnew.variants.items():
            if vid not in pold.variants:
                log(f"[NEW VARIANT] {pnew.title} ({handle}) vid={vid}")
                send_embed(desc_new(pnew), pnew.image)

        for vid, vnew in pnew.variants.items():
            vold = pold.variants.get(vid)
            if not vold:
                continue

            # 价格变化
            if abs((vnew.price or 0) - (vold.price or 0)) > 1e-6:
                log(f"[PRICE] {pnew.title} ({handle}) {vold.price} -> {vnew.price} (vid={vid})")
                send_embed(desc_price_change(pnew, vold, vnew), pnew.image)

            # 仅提醒“缺货→到货”
            if (not bool(vold.available)) and bool(vnew.available):
                log(f"[RESTOCK] {pnew.title} ({handle}) variant {vid} now available")
                send_embed(desc_restock(pnew, vnew), pnew.image)

            # 库存数量增加
            if isinstance(vnew.inventory_quantity, int) and isinstance(vold.inventory_quantity, int):
                if vnew.inventory_quantity > vold.inventory_quantity:
                    log(f"[QTY UP] {pnew.title} ({handle}) variant {vid} {vold.inventory_quantity}->{vnew.inventory_quantity}")
                    send_embed(desc_restock(pnew, vnew), pnew.image)

# ---------------- 主入口 ----------------
def list_dir(label: str):
    try:
        files = os.listdir(".")
        log(f"{label} | CWD={os.getcwd()} | files={files}")
    except Exception as e:
        log(f"list_dir error: {e}")

def main():
    log(f"START cwd={os.getcwd()}")
    log(f"ENV SNAPSHOT_PATH={SNAPSHOT_PATH} -> abs={os.path.abspath(SNAPSHOT_PATH)}")
    list_dir("BEFORE RUN")

    old = load_snapshot()
    new = build_snapshot()
    log(f"products found (new snapshot size) = {len(new)}")
    diff_and_report(old, new)

    # 无论是否有变化，一定写快照（首次必须落盘）
    save_snapshot(new)
    list_dir("AFTER SAVE")

    log(f"Done. Tracked: {len(new)} products")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        raise
