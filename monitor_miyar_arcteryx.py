# -*- coding: utf-8 -*-
"""
监控 https://store.miyaradventures.com/ 上所有 Arc'teryx 商品（变体级）
- 上新（新商品/新变体）
- 价格变化
- 库存状态变化（缺货↔到货）
- 库存数量增加（若主题暴露 inventory_quantity）
并按如下格式逐条通过 Discord Webhook 推送（右侧缩略图）：

🔔 上新提醒 🇨🇦 加拿大官网
• 名称：Atom Hoody Men's
• 货号：X000009556
• 颜色：Trail Magic
• 价格：CA$ 360
🧾 库存信息：XL:1

（右侧商品缩略图）
"""
import json
import os
import time
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

# --------- 基本配置 ----------
BASE = "https://store.miyaradventures.com/"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "snapshot.json")
USER_AGENT = "Mozilla/5.0 (compatible; MiyarArcMonitor/1.0; +https://github.com)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

# ---------- 数据模型 ----------
@dataclass
class VariantState:
    id: int
    title: str
    option1: Optional[str]  # 常为颜色
    option2: Optional[str]  # 常为尺码
    option3: Optional[str]
    sku: Optional[str]
    price: float
    compare_at_price: Optional[float]
    available: bool
    inventory_quantity: Optional[int]  # 主题不一定暴露

@dataclass
class ProductState:
    handle: str
    title: str
    vendor: Optional[str]
    url: str
    image: Optional[str]
    variants: Dict[str, VariantState]  # key: variant_id(str)

Snapshot = Dict[str, ProductState]  # key: handle


# ---------- 工具函数 ----------
def money_to_float(x) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, (int, float)):
            # 有些端点用分为单位的整数
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
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int):
            cur = cur[k] if 0 <= k < len(cur) else None
        else:
            return default
    return default if cur is None else cur

def get_json(url: str, retries: int = 3, timeout: int = 20):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(1.2 * (i + 1))
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
        time.sleep(1.2 * (i + 1))
    return None


# ---------- 商品列表抓取 ----------
def fetch_products_via_products_json(limit: int = 250) -> List[dict]:
    """优先使用 /products.json 分页抓取"""
    out = []
    page = 1
    while True:
        url = urljoin(BASE, f"/products.json?limit={limit}&page={page}")
        data = get_json(url)
        if not data or not data.get("products"):
            break
        out.extend(data["products"])
        page += 1
        time.sleep(0.5)
        if page > 40:  # 安全阈值
            break
    return out

def iter_sitemap_product_urls() -> List[str]:
    """备用方案：遍历 sitemap_products_*.xml 获取产品 URL"""
    urls = []
    idx = 1
    while True:
        url = urljoin(BASE, f"/sitemap_products_{idx}.xml")
        xml = get_text(url)
        if not xml:
            break
        try:
            root = ET.fromstring(xml)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for node in root.findall("sm:url", ns):
                loc = node.find("sm:loc", ns)
                if loc is not None and loc.text:
                    urls.append(loc.text.strip())
        except ET.ParseError:
            break
        idx += 1
        if idx > 30:
            break
        time.sleep(0.3)
    return urls

def handle_from_product_url(purl: str) -> Optional[str]:
    try:
        path = urlparse(purl).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "products":
            return parts[1]
    except Exception:
        pass
    return None

def fetch_product_js_by_handle(handle: str) -> Optional[dict]:
    return get_json(urljoin(BASE, f"/products/{handle}.js"))


# ---------- 标准化 ----------
def normalize_product_from_products_json(p: dict) -> Optional[ProductState]:
    handle = p.get("handle")
    if not handle:
        return None
    url = urljoin(BASE, f"/products/{handle}")
    image = try_get(p, "images", 0, "src")
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
            compare_at_price=money_to_float(v.get("compare_at_price")) if v.get("compare_at_price") else None,
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

def normalize_product_from_js(p: dict) -> Optional[ProductState]:
    handle = p.get("handle")
    if not handle:
        return None
    url = p.get("url") or urljoin(BASE, f"/products/{handle}")
    image = try_get(p, "images", 0)
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
            compare_at_price=money_to_float(v.get("compare_at_price")) if v.get("compare_at_price") else None,
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


# ---------- Arc'teryx 判定 ----------
def is_arcteryx(title: str, vendor: Optional[str], tags: Optional[List[str]] = None) -> bool:
    t = (title or "").lower()
    v = (vendor or "").lower()
    if "arc'teryx" in v or "arcteryx" in v:
        return True
    if "arc'teryx" in t or "arcteryx" in t:
        return True
    if tags:
        low = [x.lower() for x in tags]
        if any(("arc'teryx" in x or "arcteryx" in x) for x in low):
            return True
    return False


# ---------- 快照 ----------
def load_snapshot() -> Snapshot:
    if not os.path.exists(SNAPSHOT_PATH):
        return {}
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    snap: Snapshot = {}
    for handle, pdata in raw.items():
        variants = {vid: VariantState(**v) for vid, v in pdata["variants"].items()}
        snap[handle] = ProductState(
            handle=pdata["handle"],
            title=pdata["title"],
            vendor=pdata.get("vendor"),
            url=pdata["url"],
            image=pdata.get("image"),
            variants=variants,
        )
    return snap

def save_snapshot(snap: Snapshot):
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
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


# ---------- Discord 发送（Embed，右侧缩略图） ----------
def send_embed(description: str, thumb: Optional[str]):
    """
    标题固定：🔔 <类型> 🇨🇦 加拿大官网
    description：按要求的多行文本
    缩略图：右侧展示
    """
    if not DISCORD_WEBHOOK:
        print("[TEST MODE] would send:\n", description)
        return
    embed = {
        "title": "🔔 通知 🇨🇦 加拿大官网",  # 具体类型在 description 第一行携带
        "color": 0x2B65EC,  # 蓝色竖线
        "description": description.strip(),
    }
    if thumb:
        embed["thumbnail"] = {"url": thumb}

    payload = {"embeds": [embed]}
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    for i in range(3):
        try:
            r = SESSION.post(DISCORD_WEBHOOK, headers=headers, data=json.dumps(payload), timeout=20)
            if 200 <= r.status_code < 300:
                return
            else:
                print(f"[Discord] {r.status_code}: {r.text}")
        except requests.RequestException as e:
            print(f"[Discord error] {e}")
        time.sleep(1.2 * (i + 1))


# ---------- 消息格式化（严格按你给的样式） ----------
def format_inventory_line_for_product(p: ProductState) -> str:
    # 聚合各尺码：Size:qty（若无数量但在售，则记为1；缺货为0）
    counts: Dict[str, int] = {}
    for v in p.variants.values():
        size = v.option2 or v.option1 or "N/A"
        qty = v.inventory_quantity if isinstance(v.inventory_quantity, int) else (1 if v.available else 0)
        counts[size] = counts.get(size, 0) + max(0, int(qty))
    if not counts:
        return "无"
    # 保持稳定顺序：XXS, XS, S, M, L, XL, XXL, 其他
    order = ["XXXS","XXS","XS","S","M","L","XL","XXL","XXXL"]
    sorted_items = sorted(counts.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 999, kv[0]))
    return " | ".join([f"{k}:{v}" for k, v in sorted_items])

def description_new(p: ProductState) -> str:
    # 取一个代表变体（用于货号/颜色/价格显示）
    anyv = next(iter(p.variants.values()))
    lines = [
        "🔔 上新提醒 🇨🇦 加拿大官网",
        f"• 名称：{p.title}",
        f"• 货号：{anyv.sku or '未知'}",
        f"• 颜色：{anyv.option1 or '未知'}",
        f"• 价格：CA$ {anyv.price:.0f}" if anyv.price == int(anyv.price) else f"• 价格：CA$ {anyv.price:.2f}",
        f"🧾 库存信息：{format_inventory_line_for_product(p)}",
        "",
        # 不加链接，严格按你给的格式
        "（右侧商品缩略图）",
    ]
    return "\n".join(lines)

def description_restock(p: ProductState, v: VariantState) -> str:
    lines = [
        "🔔 补货提醒 🇨🇦 加拿大官网",
        f"• 名称：{p.title}",
        f"• 货号：{v.sku or '未知'}",
        f"• 颜色：{v.option1 or '未知'}",
        f"• 价格：CA$ {v.price:.0f}" if v.price == int(v.price) else f"• 价格：CA$ {v.price:.2f}",
        f"🧾 库存信息：{(v.option2 or 'N/A')}:{v.inventory_quantity if isinstance(v.inventory_quantity, int) else (1 if v.available else 0)}",
        "",
        "（右侧商品缩略图）",
    ]
    return "\n".join(lines)

def description_price(p: ProductState, v_old: VariantState, v_new: VariantState) -> str:
    lines = [
        "🔔 价格变化 🇨🇦 加拿大官网",
        f"• 名称：{p.title}",
        f"• 货号：{v_new.sku or '未知'}",
        f"• 颜色：{v_new.option1 or '未知'}",
        f"• 价格：CA$ {v_old.price:.2f} → CA$ {v_new.price:.2f}",
        f"🧾 库存信息：{(v_new.option2 or 'N/A')}:{v_new.inventory_quantity if isinstance(v_new.inventory_quantity, int) else (1 if v_new.available else 0)}",
        "",
        "（右侧商品缩略图）",
    ]
    return "\n".join(lines)


# ---------- 构建最新快照 ----------
def build_snapshot() -> Snapshot:
    snap: Snapshot = {}
    products = fetch_products_via_products_json()
    if products:
        for p in products:
            if not is_arcteryx(p.get("title",""), p.get("vendor"), p.get("tags", [])):
                continue
            ps = normalize_product_from_products_json(p)
            if not ps:
                continue
            # 再用 .js 补齐 inventory_quantity/available 准确性
            js = fetch_product_js_by_handle(ps.handle)
            if js:
                jsn = normalize_product_from_js(js)
                if jsn:
                    # 合并：以 js 为准
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
        h = handle_from_product_url(u)
        if not h:
            continue
        time.sleep(0.25)
        js = fetch_product_js_by_handle(h)
        if not js:
            continue
        if not is_arcteryx(js.get("title",""), js.get("vendor"), js.get("tags", [])):
            continue
        ps = normalize_product_from_js(js)
        if ps:
            snap[ps.handle] = ps
    return snap


# ---------- Diff & 推送 ----------
def diff_and_report(old: Snapshot, new: Snapshot):
    # 新商品
    for handle, p in new.items():
        if handle not in old:
            send_embed(description_new(p), p.image)

    # 变体新增 / 价格变化 / 库存状态变化 / 数量增加
    for handle, pnew in new.items():
        pold = old.get(handle)
        if not pold:
            continue

        # 新增变体 => 视为“上新提醒”
        for vid, vnew in pnew.variants.items():
            if vid not in pold.variants:
                send_embed(description_new(pnew), pnew.image)

        for vid, vnew in pnew.variants.items():
            vold = pold.variants.get(vid)
            if not vold:
                continue

            # 价格变化
            if abs((vnew.price or 0) - (vold.price or 0)) > 1e-6:
                send_embed(description_price(pnew, vold, vnew), pnew.image)

            # 库存状态变化（缺货→到货 or 反向）
            if bool(vnew.available) != bool(vold.available):
                # 只有到货才提醒（更贴近“补货提醒”语义）
                if vnew.available:
                    send_embed(description_restock(pnew, vnew), pnew.image)

            # 库存数量增加（两边都有数量才比较）
            if isinstance(vnew.inventory_quantity, int) and isinstance(vold.inventory_quantity, int):
                if vnew.inventory_quantity > vold.inventory_quantity:
                    send_embed(description_restock(pnew, vnew), pnew.image)


# ---------- 主入口 ----------
def main():
    old = load_snapshot()
    new = build_snapshot()
    diff_and_report(old, new)
    save_snapshot(new)
    print(f"Done. Products tracked: {len(new)}")

if __name__ == "__main__":
    main()
