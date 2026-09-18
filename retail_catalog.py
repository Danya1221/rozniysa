"""Retail product identity, price messages and brand navigation."""
import hashlib
import html
import re
from collections import OrderedDict
from decimal import Decimal

from prices import (display_title, identity, marked_price, price_text, iphone_model,
                    activation_state, SIM_LABELS, SIM_ORDER, iphone_storage_rank, units)
from retail_store import stable_id


def natural(value):
    return tuple((0, int(p)) if p.isdigit() else (1, p.casefold()) for p in re.split(r"(\d+)", value))


def brand_section(item):
    model = iphone_model(item.title)
    if item.block in {"CPO", "ASIS"}:
        return "Apple", item.block
    if model:
        number = re.search(r"\d+", model)
        return "Apple", "iPhone " + (number[0] if number else ("17" if "Air" in model else "SE"))
    block = item.block
    if block.startswith("Apple Watch"):
        return "Apple", "Apple Watch"
    if block in {"AirPods", "iPad", "MacBook / iMac", "Mac mini", "Mac Studio", "Apple TV", "AirTag", "Apple Accessories", "Apple"}:
        return "Apple", "Аксессуары Apple" if block in {"Apple Accessories", "AirTag", "Apple TV", "Apple"} else block
    if block.startswith("Samsung"):
        section = "Samsung"
        for pattern, label in [(r"\b(?:Fold|Flip)\b", "Galaxy Fold / Flip"),
                               (r"\bTab\b", "Планшеты"), (r"\bWatch\b", "Часы"),
                               (r"\bBuds\b", "Наушники"), (r"\bA\s*\d{2,3}\b", "Galaxy A"),
                               (r"\bS\s*\d{2}\b", "Galaxy S")]:
            if re.search(pattern, item.title, re.I):
                section = label
                break
        return "Samsung", section
    return block or "Другая техника", block or "Другая техника"


def to_product(item, settings, options=None):
    title = display_title(item)
    brand, section = brand_section(item)
    return {"id": stable_id(identity(title, item.sim, item.currency)), "title": title,
            "price": str(marked_price(item, settings, options)), "currency": item.currency,
            "brand": brand, "section": section, "model": iphone_model(title), "sim": item.sim,
            "condition": activation_state(title),
            "storage_rank": iphone_storage_rank(item) if iphone_model(title) else 0}


def dedupe_products(products):
    """Collapse customer-visible ID collisions, keeping the lowest current price."""
    unique = OrderedDict()
    for product in products:
        product_id = product["id"]
        current = unique.get(product_id)
        if current is None or Decimal(product["price"]) < Decimal(current["price"]):
            unique[product_id] = product
    return list(unique.values())


def dedupe_products(products):
    """Collapse customer-visible ID collisions, keeping the lowest retail price."""
    unique = OrderedDict()
    for product in products:
        current = unique.get(product["id"])
        if current is None:
            unique[product["id"]] = product
            continue
        try:
            current_price = Decimal(current["price"])
            candidate_price = Decimal(product["price"])
        except Exception as exc:
            raise ValueError("Некорректная цена у повторяющейся позиции") from exc
        if candidate_price < current_price:
            unique[product["id"]] = product
    return list(unique.values())


def section_order(value):
    presets = ["iPhone 11", "iPhone 12", "iPhone 13", "iPhone 14", "iPhone 15", "iPhone 16", "iPhone 17",
               "Apple Watch", "MacBook / iMac", "iPad", "AirPods", "Аксессуары Apple", "Galaxy A", "Galaxy S",
               "Galaxy Fold / Flip", "Планшеты", "Часы", "Наушники"]
    return presets.index(value) if value in presets else len(presets), natural(value)


def brand_order(value):
    names = ["Apple", "Samsung", "Xiaomi", "Honor", "Huawei", "Realme", "Tecno"]
    return names.index(value) if value in names else len(names), natural(value)


def product_sort(p):
    return (p.get("condition") == "active", natural(p.get("model", "")),
            SIM_ORDER.get(p.get("sim", "unknown"), 99), p.get("storage_rank", 0), natural(p["title"]))


def render_prices(products, username, preferred=()):
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username or ""):
        raise ValueError("Укажи ORDER_BOT_USERNAME: username бота оформления без @")
    groups = {}
    for p in products:
        groups.setdefault((p["brand"], p["section"]), []).append(p)
    pages, navigation = OrderedDict(), OrderedDict()
    ordered = sorted(groups, key=lambda pair: (brand_order(pair[0]), section_order(pair[1])))
    if preferred:
        ranks = {name: i for i, name in enumerate(preferred)}
        ordered.sort(key=lambda pair: ranks.get(pair[1], len(ranks)))
    for brand, section in ordered:
        heading = "<b>" + html.escape(section) + "</b>"
        prefix = stable_id(brand + "|" + section)
        index, last_group = 0, None
        body = heading + "\n\n"
        entry = {"section": section, "keys": []}
        navigation.setdefault(brand, []).append(entry)
        for product in sorted(groups[(brand, section)], key=product_sort):
            group = None
            if product.get("model"):
                group = (product["model"], "Активированное" if product.get("condition") == "active" else "Не активированное",
                         SIM_LABELS.get(product.get("sim"), ""))
            subgroup = "<b>" + html.escape(" · ".join(x for x in group if x)) + "</b>\n" if group else ""
            url = f'https://t.me/{username}?start=p_{product["id"]}'
            label = product["title"] + " — " + price_text(Decimal(product["price"]), product["currency"])
            line = f'<a href="{url}">{html.escape(label)}</a>\n'
            addition = (("\n" if last_group else "") + subgroup if group != last_group else "") + line
            if units(body + addition) > 3600 and body != heading + "\n\n":
                key = prefix + ":" + str(index)
                pages[key] = body.rstrip()
                entry["keys"].append(key)
                index += 1
                body = heading + f"\nЧасть {index+1}\n\n"
                addition = subgroup + line
            if units(body + addition) > 3900:
                raise ValueError("Слишком длинное название позиции: " + product["title"][:70])
            body += addition
            last_group = group
        key = prefix + ":" + str(index)
        pages[key] = body.rstrip()
        entry["keys"].append(key)
    return pages, navigation


def cover_bytes(brand):
    """Built-in category artwork. An administrator may upload a replacement photo."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1200, 640))
    draw = ImageDraw.Draw(image)
    accent = (255, 139, 75) if brand == "Apple" else (89, 149, 244)
    for y in range(640):
        draw.line((0, y, 1200, y), fill=(15+y//50, 20+y//45, 29+y//35))
    for x, y, w, h in ((790,110,190,370), (1000,230,110,220), (660,275,105,185)):
        draw.rounded_rectangle((x,y,x+w,y+h), radius=26, fill=(35,43,58), outline=accent, width=3)
        draw.rounded_rectangle((x+8,y+8,x+w-8,y+h-8), radius=20, fill=(23,31,44))
    try:
        large = ImageFont.truetype("DejaVuSans.ttf", 78 if len(brand) < 14 else 48)
        small = ImageFont.truetype("DejaVuSans.ttf", 24)
    except OSError:
        large = small = ImageFont.load_default(size=32)
    draw.rounded_rectangle((65,83,145,91), radius=4, fill=accent)
    draw.text((65,170), brand[:30], font=large, fill="white")
    draw.text((68,295), "ВЫБЕРИ СВОЁ УСТРОЙСТВО", font=small, fill=(162,176,193))
    draw.line((65,500,535,500), fill=(57,69,88), width=2)
    draw.text((68,527), "ТЕХНИКА · РОЗНИЦА", font=small, fill=accent)
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=88)
    return stream.getvalue()
