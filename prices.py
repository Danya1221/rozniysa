"""Price parsing/rendering without Telegram or network side effects."""
import hashlib
import html
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

SPACE = re.compile(r"\s+")
FLAGS = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
CLOSED = re.compile(r"(?:мы\s+)?(?:сейчас\s+|временно\s+)?закрыты|продажи\s+закрыты|"
                    r"рабочий\s+день\s+окончен|магазин\s+закрыт|не\s+работаем|"
                    r"при[её]м\s+заказов\s+(?:заверш[её]н|закрыт)", re.I)
UNAVAILABLE = re.compile(r"нет\s+в\s+наличии|нет\s+на\s+складе|закончились|продано|❌", re.I)
WHOLESALE = re.compile(r"\b(?:от|from)\s*\d+\s*(?:шт|штук|pcs)\b", re.I)
PRICE = re.compile(
    r"(?P<prefix>[$€₽])?\s*"
    r"(?P<amount>(?:\d{1,3}(?:[ ,.]\d{3})+|\d{1,7})(?:[.,]\d{1,2})?)"
    r"\s*(?P<currency>₽|руб\.?|р\.|RUB|USD|\$|EUR|€)?"
    r"\s*(?P<flags>(?:[\U0001F1E6-\U0001F1FF]{2}\s*)*)[✅🔥‼️!]*$",
    re.I,
)
ACCESSORY = re.compile(r"акс(?:ессуар|ис)|чехол|стекло|кабел[ья]|кабель|адаптер|[АA]dapter|переходник|зарядк|"
                       r"ремеш|бампер|case\b|charger\b|cable\b", re.I)
ASIS = re.compile(r"\bAS[\s-]?IS\b|\bASIS\b|асис", re.I)
CPO = re.compile(r"\bCPO\b", re.I)
PACKAGING_NOTE = r"ориг(?:инальная)?\.?\s*упак(?:овка)?\.?"
ORIGINAL_PACKAGING = re.compile(
    r"\(\s*" + PACKAGING_NOTE + r"(?:\s*iphone)?\s*\)|\b" + PACKAGING_NOTE + r"(?!\w)", re.I,
)
INACTIVE = re.compile(r"\bне[\s-]*актив\w*|\binactive\b|not[\s-]*activated|не[\s-]*активирован\w*", re.I)
ACTIVE = re.compile(r"\bактив\w*|\bactive\b|\bactivated\b|pre[\s-]*activated|предактив\w*", re.I)
IPHONE = re.compile(r"\b(?:iphone|айфон)\s*:?\s*(\d{1,2}\s*(?:e\b|pro\s*max\b|pro\b|"
                    r"plus\b|mini\b|air\b)?|air\b|se(?:\s*\d)?)", re.I)
SHORT_IPHONE = re.compile(r"^(\d{1,2}(?:e)?(?:\s+(?:pro\s+max|pro|plus|mini|air))?)\s+(?=\d{1,4}\s*(?:gb|tb|гб|тб)?\b)", re.I)
BRANDS = (
    ("Ray-Ban Meta", r"ray[\s-]?ban|wayfarer|skyler"),
    ("Oura Ring", r"\boura(?:\s+ring)?\b"),
    ("LEGO", r"\blego\b|лего"),
    ("Dyson", r"\bdyson\b|дайсон"),
    ("Canon", r"\bcanon\b|кэнон|канон"),
    ("Rode", r"\br(?:o|ø)de\b"),
    ("COROS", r"\bcoros\b"),
    ("DJI / Insta360", r"\bdji\b|\binsta\s*360\b"),
    ("Kodak / Fujifilm", r"\bkodak\b|\bfujifilm\b"),
    ("Bowers & Wilkins", r"bowers|b&w|\bpx[78]\b"),
    ("Harman Kardon / Bose", r"harman\s*kardon|\bbose\b|\baura\s+studio\b|\bonyx\b|soundsticks"),
    ("Google", r"\bgoogle\b|\bpixel\b|\bfitbit\b"),
    ("Samsung", r"\bsamsung\b|\bgalaxy\b|самсунг"),
    ("Apple", r"\bapple\b|\biphone\b|айфон|\bipad\b|\bmacbook\b|\bairpods\b|\bimac\b|"
              r"\bmac\s+mini\b|\bmac\s+studio\b|\bapple\s*tv\b|\bairtag\b|apple\s*watch"),
    ("Sony", r"\bsony\b"),
    ("Xiaomi", r"\bxiaomi\b|\bredmi\b|\bpoco\b|^\s*(?:redmi\s+)?note\s+\d{1,2}[a-z]*\b"),
    ("Huawei", r"\bhuawei\b"),
    ("Honor", r"\bhonor\b"),
    ("Realme", r"\brealme\b|реалми"),
    ("Tecno", r"\btecno\b|текно"),
    ("Infinix", r"\binfinix\b"),
    ("OnePlus", r"\bone\s*plus\b"),
    ("OPPO", r"\boppo\b"),
    ("Vivo", r"\bvivo\b|\biqoo\b"),
    ("Nothing", r"\bnothing\b|\bcmf\b"),
    ("Nubia", r"\bnubia\b|red\s*magic"),
    ("Garmin", r"\bgarmin\b"),
    ("GoPro", r"\bgopro\b"),
    ("Marshall", r"\bmarshall\b"),
    ("Anker", r"\banker\b|\bsoundcore\b"),
    ("JBL", r"\bjbl\b"),
    ("Nintendo", r"\bnintendo\b|\bswitch\b"),
    ("PlayStation", r"playstation|\bps5\b"),
    ("Xbox", r"\bxbox\b"),
)
SIM_LABELS = {
    "sim": "SIM",
    "hybrid": "SIM + eSIM",
    "esim": "eSIM",
    "dual": "2 SIM",
    "unknown": "",
}
# Unlabelled items precede named SIM sections so they cannot appear under a
# misleading eSIM / physical SIM heading.
SIM_ORDER = {"unknown": -1, "hybrid": 0, "dual": 1, "esim": 2, "sim": 3}
CONDITION_LABELS = {"inactive": "Не активированное", "active": "Актив", "unknown": "Не активированное"}
CONDITION_ORDER = {"inactive": 0, "active": 1, "unknown": 2}


def clean(text):
    return SPACE.sub(" ", unicodedata.normalize("NFKC", text).replace("\u200b", "")).strip()


def strip_decoration(text):
    return clean(text).strip(" \t•▫▪◽◾🔹🔸📱📦🎧⌚🔥✅*_-—–")


def currency_name(value, default="RUB"):
    return {"₽": "RUB", "РУБ": "RUB", "РУБ.": "RUB", "Р.": "RUB",
            "$": "USD", "€": "EUR"}.get(value.upper(), value.upper()) if value else default


def amount_value(value):
    value = value.replace(" ", "")
    fraction = re.search(r"([.,]\d{1,2})$", value)
    if fraction:
        whole = value[:fraction.start()].replace(",", "").replace(".", "")
        return Decimal(whole + "." + fraction[0][1:])
    return Decimal(value.replace(",", "").replace(".", ""))


def split_price(line, default_currency):
    original = strip_decoration(line)
    if UNAVAILABLE.search(original) or IPHONE.fullmatch(original):
        return None
    wholesale = WHOLESALE.search(original)
    if wholesale:
        original = original[:wholesale.start()].rstrip(" ,;/—-")
    match = PRICE.search(original)
    if not match:
        return None
    title = original[:match.start()].strip()
    separated = bool(re.search(r"[-—–=:]\s*$", title))
    amount = amount_value(match["amount"])
    if not separated and not (match["currency"] or match["prefix"]) and amount < 1000:
        return None
    title = title.rstrip(" -—–=:|")
    if not title or not re.search(r"[A-Za-zА-Яа-яЁё]", title):
        return None
    if re.search(r"https?://|@\w+|доставка|гарантия|телефон:|заказ(?:ы|ов)?\s*:|"
                 r"итого|скидка|минимальн|курс\s|обновл[её]н", title, re.I):
        return None
    if amount <= 0 or amount > 10_000_000:
        return None
    flags = match["flags"].strip()
    if flags:
        title += " " + flags
    currency = currency_name(match["currency"] or match["prefix"], default_currency)
    return title, amount, currency


def sim_type(text):
    text = clean(text).lower()
    text = re.sub(r"\be[\s-]+sim\b", "esim", text)
    if re.search(
        r"\b(?:1\s*)?(?:nano\s*)?sim\s*(?:\+|/|&|and|и)\s*esim\b|"
        r"\besim\s*(?:\+|/|&|and|и)\s*(?:1\s*)?(?:nano\s*)?sim\b",
        text,
    ):
        return "hybrid"
    if re.search(r"\b(?:2|dual)\s*(?:nano\s*)?sim\b|две\s*sim|\b2x\s*sim\b", text):
        return "dual"
    if re.search(r"\besim\b|есим", text):
        return "esim"
    if re.search(r"\b(?:1\s*)?(?:nano\s*)?sim\b|physical\s*sim|физическ\w*\s*sim", text):
        return "sim"
    return "unknown"


def iphone_model(title):
    match = IPHONE.search(title)
    if not match:
        return ""
    suffix = clean(match[1])
    suffix = re.sub(r"\s+e\b", "e", suffix, flags=re.I)
    words = [x if re.fullmatch(r"\d+e?", x, re.I) else x.title() for x in suffix.split()]
    model = " ".join(words)
    if model.casefold() == "17 air":
        return "iPhone Air"
    return "iPhone " + model


def iphone_publish_block(model):
    """Logical iPhone block. Physical Telegram messages are grouped later."""
    return model or ""


def iphone_storage_key(item):
    title = FLAGS.sub("", clean(item.title))
    model = iphone_model(title)
    if model:
        title = re.sub(re.escape(model), "", title, count=1, flags=re.I).strip()
    match = re.search(r"\b(\d{1,4})\s*(GB|TB)?\b", title, re.I)
    if not match:
        return ""
    value = int(match.group(1))
    unit = (match.group(2) or "GB").upper()
    return f"{value}{unit}"

def brand_of(text):
    for name, pattern in BRANDS:
        if re.search(pattern, text, re.I):
            return name
    return ""


def apple_watch_block(title):
    plain = FLAGS.sub("", clean(title)).strip()

    # Explicitly branded non-Apple watches always win over generic watch rules.
    if re.search(
        r"\b(?:galaxy|samsung|one\s*plus|oneplus|xiaomi|redmi|huawei|honor|"
        r"garmin|coros|pixel|fitbit|amazfit|oppo|vivo|realme|nothing)\b",
        plain,
        re.I,
    ):
        return ""

    # Supplier shorthand for Apple Watch. A Series shorthand must be followed
    # by a real watch case size so Galaxy S-series phones are never mistaken
    # for Apple Watch.
    watch_size = r"(?:38|40|41|42|44|45|46|49)(?:\s*mm)?"
    if re.search(r"^S\s*\d{1,2}\s+" + watch_size + r"\b", plain, re.I):
        return "Apple Watch"
    if re.search(r"^SE\s*\d*\s+" + watch_size + r"\b", plain, re.I):
        return "Apple Watch"
    if re.search(r"^(?:UL|ULTRA)\s*\d{1,2}\b", plain, re.I):
        return "Apple Watch"

    if re.search(r"\bapple\s*watch\b|^watch\b", plain, re.I):
        return "Apple Watch"
    return ""


def samsung_block(title):
    plain = FLAGS.sub("", clean(title)).strip()
    has_brand = bool(re.search(r"\bsamsung\b|\bgalaxy\b|самсунг", plain, re.I))

    # Supplier often omits "Samsung" from every product row.
    if re.search(r"^(?:samsung\s+|galaxy\s+)?buds\s*[34]\b", plain, re.I):
        return "Samsung Buds"
    if re.search(r"^(?:samsung\s+|galaxy\s+)?a\s*(?:17|27|37|57)\b", plain, re.I):
        return "Samsung A + S25"
    if re.search(r"^(?:samsung\s+|galaxy\s+)?s\s*25(?:\s*(?:fe|edge|ultra|\+|plus))?\b", plain, re.I):
        return "Samsung A + S25"
    if re.search(r"^(?:samsung\s+|galaxy\s+)?s\s*26(?:\s*(?:fe|edge|ultra|\+|plus))?\b", plain, re.I):
        return "Samsung S26"
    if re.search(r"^(?:samsung\s+|galaxy\s+)?tab\s*s\s*\d*", plain, re.I):
        return "Samsung Tab S"
    if re.search(r"^(?:samsung\s+|galaxy\s+)?(?:z\s*)?(?:fold|flip)\b", plain, re.I):
        return "Samsung Fold / Flip"

    if not has_brand:
        return ""
    if re.search(r"\b(?:galaxy\s*)?tab\s*s\s*\d*", plain, re.I):
        return "Samsung Tab S"
    if re.search(r"\b(?:galaxy\s*)?(?:z\s*)?(?:fold|flip)\b", plain, re.I):
        return "Samsung Fold / Flip"
    if re.search(r"\b(?:galaxy\s*)?buds\s*[34]\b", plain, re.I):
        return "Samsung Buds"
    if re.search(r"\b(?:galaxy\s+|samsung\s+(?:galaxy\s+)?)?a\s*(?:17|27|37|57)\b", plain, re.I):
        return "Samsung A + S25"
    if re.search(r"\bgalaxy\s+s\s*25\b", plain, re.I):
        return "Samsung A + S25"
    if re.search(r"\bgalaxy\s+s\s*26\b", plain, re.I):
        return "Samsung S26"
    return "Samsung"


def apple_ipad_block(title):
    """Recognize supplier iPad rows that omit the word iPad."""
    plain = FLAGS.sub("", clean(title)).strip()
    if re.search(r"\bApple\s+Pencil\b", plain, re.I):
        return "Apple Accessories"
    if re.search(r"\biPad\b", plain, re.I):
        return "iPad"
    # Supplier examples:
    # MINI 7 128 ... Wi-Fi
    # AIR 11/13 M3/M4 128/256 ... Wi-Fi
    # PRO 11/13 M4/M5 256/1TB ... Wi-Fi/LTE
    # PRO 12.9 M2 128 ... LTE
    if re.search(r"^MINI\s+\d+\s+(?:\d{2,4}|\d+TB)\b.*\b(?:Wi[ -]?Fi|LTE|Cellular)\b", plain, re.I):
        return "iPad"
    if re.search(r"^(?:AIR|PRO)\s+(?:11|12\.9|13)\s+M\d+\s+(?:\d{2,4}|\d+TB)\b.*\b(?:Wi[ -]?Fi|LTE|Cellular)\b", plain, re.I):
        return "iPad"
    return ""


def apple_computer_block(title):
    """Recognize supplier MacBook rows even when the word MacBook is omitted."""
    plain = FLAGS.sub("", clean(title)).strip()
    if re.search(r"\b(?:MacBook|iMac)\b", plain, re.I):
        return "MacBook / iMac"

    # Supplier examples: Neo 13 MHFD4 ... (A18 Pro 8/256),
    # Air 13/15 MDH... (M5 ...), Pro 14/16 ... (M4/M5 Pro ...).
    sku = r"[A-Z0-9]{4,}"
    chip = r"(?:A18\s*Pro|M\d+(?:\s*(?:Pro|Max))?)"
    if re.search(r"^(?:Neo|Air)\s+(?:13|15)\s+" + sku + r"\b.*\(" + chip + r"\b", plain, re.I):
        return "MacBook / iMac"
    if re.search(r"^Pro\s+(?:14|16)\s+" + sku + r"\b.*\(" + chip + r"\b", plain, re.I):
        return "MacBook / iMac"
    return ""


def apple_accessory_block(title):
    """Apple power adapters / MacBook transitions belong to the common Apple post."""
    plain = FLAGS.sub("", clean(title)).strip()
    mixed_adapter = r"(?:Adapter|Аdapter|адаптер)"
    if "" in title and re.search(mixed_adapter, plain, re.I):
        return "Apple Accessories"
    if re.search(r"\bпереходник\s+для\s+MacBook\b", plain, re.I):
        return "Apple Accessories"
    if re.search(r"^" + mixed_adapter + r"\s+(?:universal|20W|USB[ -]?C\s+to\s+USB)\b", plain, re.I):
        return "Apple Accessories"
    return ""


def product_block(title):
    watch = apple_watch_block(title)
    if watch:
        return watch
    samsung = samsung_block(title)
    if samsung:
        return samsung
    ipad = apple_ipad_block(title)
    if ipad:
        return ipad
    apple_accessory = apple_accessory_block(title)
    if apple_accessory:
        return apple_accessory
    computer = apple_computer_block(title)
    if computer:
        return computer
    for label in ("AirPods", "iPad", "Mac mini", "Mac Studio", "Apple TV", "AirTag"):
        if re.search(r"\b" + re.escape(label) + r"\b", title, re.I):
            return label
    return brand_of(title)


def ordered_blocks(blocks, preferred=()):
    defaults = ["iPhone", "AirPods", "Apple Accessories", "Mac mini", "Apple TV", "AirTag",
                "Apple Watch", "iPad", "MacBook / iMac", "Mac Studio", "Apple", "Ray-Ban Meta", "Samsung", "Honor", "Realme", "Huawei", "Tecno",
                "Xiaomi", "Google", "COROS", "Rode", "Dyson", "Oura Ring", "CPO", "ASIS", "Аксессуары", "Товары"]
    def order_key(name):
        if name in preferred:
            return (-1, preferred.index(name), (), "")
        model = iphone_model(name)
        family = "iPhone" if model else ("Apple Watch" if name.startswith("Apple Watch") else ("Samsung" if name.startswith("Samsung") else name))
        rank = defaults.index(family) if family in defaults else len(defaults) - 4
        samsung_rank = {"Samsung Buds": 0, "Samsung A + S25": 1, "Samsung S26": 2, "Samsung Tab S": 3, "Samsung Fold / Flip": 4, "Samsung": 5}.get(name, 0) if family == "Samsung" else 0
        numbered = model if model else (name if family == "Apple Watch" else "")
        number = re.search(r"\d+", numbered)
        return (rank, samsung_rank, -int(number[0]) if number else 0, tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name.casefold())), name)
    return sorted(set(blocks), key=order_key)


def normal_title(title, context=""):
    title = strip_decoration(title)
    title = re.sub(r"\bSM-[A-Za-z0-9/]+\b", "", title, flags=re.I)
    title = re.sub(r"\bайфон\b", "iPhone", title, flags=re.I)
    plain = FLAGS.sub("", title).strip()
    if SHORT_IPHONE.match(plain):
        # A flag in the middle means the flag-free text is not a substring.
        # Insert before the model, never at find()'s -1 position near the end.
        pos = re.search(r"\d", title).start()
        title = title[:pos] + "iPhone " + title[pos:]
    elif context and not brand_of(title) and not apple_ipad_block(title) and not apple_computer_block(title) and not apple_accessory_block(title) and not apple_watch_block(title):
        # Samsung block labels are navigation names, not product-name prefixes.
        # Prefix bare A/S/Tab rows with the brand only, otherwise rows become e.g.
        # "Samsung A + S25 S25 ...".
        prefix = "Samsung" if context.startswith("Samsung") else context
        title = prefix + " " + title
    title = re.sub(r"\b(\d+)\s*(?:GB|ГБ)\b", r"\1GB", title, flags=re.I)
    title = re.sub(r"\b(\d+)\s*(?:TB|ТБ)\b", r"\1TB", title, flags=re.I)
    if re.search(r"ray[\s-]?ban|wayfarer|skyler", title, re.I):
        title = re.sub(r"\bS\s*50\b", "M", title, flags=re.I)
        title = re.sub(r"\bS\s*53\b", "L", title, flags=re.I)
        if not re.search(r"ray[\s-]?ban", title, re.I):
            title = "Ray-Ban Meta " + title
    title = re.sub(r"^UL\s*(\d{1,2})\b", r"Ultra \1", title, flags=re.I)
    return clean(title)


def special_block(text):
    if ASIS.search(text):
        return "ASIS"
    if CPO.search(text):
        return "CPO"
    return ""


def activation_state(text):
    if INACTIVE.search(text):
        return "inactive"
    if ACTIVE.search(text):
        return "active"
    return "unknown"


def identity(title, sim, currency):
    flags = "".join(sorted(FLAGS.findall(title)))
    body = FLAGS.sub("", clean(title).lower())
    body = re.sub(r"(\d+)\s*(?:gb|гб)\b", r"\1", body)
    body = re.sub(r"[^\w+/]+", " ", body)
    return clean(body) + "|" + flags + "|" + sim + "|" + currency


@dataclass(frozen=True)
class Item:
    title: str
    price: Decimal
    currency: str
    block: str
    sim: str = "unknown"
    accessory: bool = False

    @property
    def key(self):
        return identity(self.title, self.sim, self.currency)

    def to_dict(self):
        return dict(title=self.title, price=str(self.price), currency=self.currency,
                    block=self.block, sim=self.sim, accessory=self.accessory)

    @classmethod
    def from_dict(cls, value):
        data = {**value, "price": Decimal(value["price"])}
        data["title"] = normal_title(data["title"])
        explicit_sim = sim_type(data["title"])
        if explicit_sim != "unknown":
            data["sim"] = explicit_sim
        if data.get("block") not in {"ASIS", "CPO", "Аксессуары"}:
            model = iphone_model(data["title"])
            known = iphone_publish_block(model) if model else product_block(data["title"])
            if known:
                data["block"] = known
        return cls(**data)


@dataclass
class ParseResult:
    items: list
    rejected: list
    closed: bool = False
    unavailable: int = 0


def price_lines(documents):
    """Join product titles with a following retail price, including across messages."""
    pending = None
    for document in documents:
        for raw in document.splitlines():
            line = strip_decoration(raw)
            if not line:
                continue
            tier = WHOLESALE.search(line)
            if tier and not FLAGS.sub("", line[:tier.start()]).strip():
                if not re.search(r"\b(?:от|from)\s*1\s", tier[0], re.I):
                    continue
                line = line[:tier.start()] + line[tier.end():]
            else:
                line = re.sub(r"\b(?:от|from)\s*1\s*(?:шт|штук|pcs)\b", "", line, flags=re.I)
            candidate = FLAGS.sub("", line).strip(" —-–=:•")
            if pending and PRICE.fullmatch(candidate) and not UNAVAILABLE.search(line):
                flags = " ".join(FLAGS.findall(line))
                yield pending + (" " + flags if flags and flags not in pending else "") + " — " + candidate
                pending = None
            else:
                if pending is not None:
                    yield pending
                pending = line
    if pending is not None:
        yield pending


def parse_documents(documents, default_currency="RUB"):
    items = OrderedDict()
    rejected = []
    context = ""
    section = ""
    section_sim = "unknown"
    section_condition = "unknown"
    closed = False
    unavailable = 0
    for raw_line in price_lines(documents):
        line = strip_decoration(raw_line)
        if not line:
            continue
        if CLOSED.search(line):
            closed = True
            continue
        if UNAVAILABLE.search(line):
            stripped = strip_decoration(UNAVAILABLE.sub("", line)).rstrip(" —-–=:")
            split = split_price(stripped, default_currency)
            unavailable_title = normal_title(split[0] if split else stripped, context)
            if split or (re.search(r"\d", unavailable_title) and (iphone_model(unavailable_title) or product_block(unavailable_title))):
                unavailable += 1
            if section_condition == "active" and activation_state(unavailable_title) == "unknown":
                unavailable_title += " Актив"
            for key, item in list(items.items()):
                if clean(item.title).casefold() == clean(unavailable_title).casefold():
                    del items[key]
            continue
        condition_header = activation_state(line)
        if condition_header != "unknown" and len(line) < 45 and not re.search(r"\d", line):
            section_condition = condition_header
            continue
        price = split_price(line, default_currency)
        if price is None:
            model = iphone_model(line)
            brand = product_block(line)
            special = special_block(line)
            is_header = len(line) <= 80 and not re.search(r"https?://|@\w+|[-—=]\s*\d{3}", line)
            if is_header and (model or brand or ACCESSORY.search(line) or special):
                context = model or (brand if brand != "Apple" else line.strip(":"))
                if ACCESSORY.search(line):
                    section = "Аксессуары"
                elif special:
                    section = special
                else:
                    section = model or brand
                section_sim = sim_type(line)
                section_condition = activation_state(line)
            elif is_header and re.fullmatch(r"(?:[12]\s*)?(?:e[\s-]?)?sim(?:\s*[+/&]\s*(?:[12]\s*)?(?:e[\s-]?)?sim)?", line, re.I):
                section_sim = sim_type(line)
            elif re.search(r"\d", line) and not re.search(r"https?://|@\w+|^\+?\d[\d ()-]{8,}$", line):
                rejected.append(line)
            continue
        title, amount, currency = price
        title = normal_title(title, context)
        if section_condition == "active" and activation_state(title) == "unknown":
            title += " Актив"
        own_brand = product_block(title)
        apple_accessory = own_brand == "Apple Accessories"
        accessory = (bool(ACCESSORY.search(title)) or (section == "Аксессуары" and not own_brand)) and not apple_accessory
        model = iphone_model(title)
        special = special_block(title)
        if not special and section in {"ASIS", "CPO"}:
            special = section
        publish_model = iphone_publish_block(model) if model else ""
        block = "Аксессуары" if accessory else (special or publish_model or own_brand or section or "Товары")
        sim = sim_type(title)
        if sim == "unknown":
            sim = section_sim if model else "unknown"
        item = Item(title, amount, currency, block, sim, accessory)
        items[item.key] = item
    return ParseResult(list(items.values()), rejected, closed and not items, unavailable)


def matches_any(text, patterns):
    return any(clean(p).casefold() in clean(text).casefold() for p in patterns)


def select_items(items, settings, overrides=None):
    overrides = overrides or {}
    sim_filter = overrides.get("sim_filter", settings.sim_filter)
    disabled = set(overrides.get("disabled_blocks", []))
    include_blocks = overrides.get("include_blocks", settings.include_blocks)
    exclude_blocks = overrides.get("exclude_blocks", settings.exclude_blocks)
    include_items = overrides.get("include_items", settings.include_items)
    exclude_items = overrides.get("exclude_items", settings.exclude_items)
    allow_accessories = overrides.get("allow_accessories", settings.allow_accessories)
    result = []
    for item in items:
        if item.block in disabled:
            continue
        if include_blocks and not matches_any(item.block, include_blocks):
            continue
        if matches_any(item.block, exclude_blocks):
            continue
        if include_items and not matches_any(item.title, include_items):
            continue
        if matches_any(item.title, exclude_items):
            continue
        if item.accessory and not allow_accessories:
            continue
        if iphone_model(item.title) and sim_filter != "all":
            if sim_filter == "sim" and item.sim not in {"sim", "dual", "hybrid"}:
                continue
            if sim_filter == "esim" and item.sim != "esim":
                continue
            if sim_filter not in {"sim", "esim"} and item.sim != sim_filter:
                continue
        result.append(item)
    return result


def merge_sources(sources):
    """First source wins, but differing country/SIM/condition/storage stay distinct."""
    merged = OrderedDict()
    for items in sources:
        for item in items:
            merged.setdefault(item.key, item)
    return list(merged.values())


def marked_price(item, settings, overrides=None):
    overrides = overrides or {}
    fixed = Decimal(str(overrides.get("markup", settings.markup)))
    percent = Decimal(str(overrides.get("markup_percent", settings.markup_percent)))
    value = item.price * (1 + percent / 100) + fixed
    if value <= 0:
        raise ValueError("После наценки получилась нулевая или отрицательная цена")
    return value.quantize(Decimal("1") if item.currency == "RUB" else Decimal("0.01"), rounding=ROUND_HALF_UP)


def price_text(value, currency):
    places = 0 if currency == "RUB" or value == value.to_integral() else 2
    number = f"{value:,.{places}f}".replace(",", " ")
    if currency == "RUB":
        return number
    return number + " " + {"USD": "$", "EUR": "€"}[currency]


def units(text):
    return len(text.encode("utf-16-le")) // 2


def item_sort(item):
    size = 0 if re.search(r"\bM\b", item.title) else 1 if re.search(r"\bL\b", item.title) else 2
    plain = FLAGS.sub("", clean(item.title)).strip().casefold()
    return size, plain


def samsung_a_s25_sort(item):
    plain = FLAGS.sub("", clean(item.title)).strip()
    a = re.search(r"\bA\s*(17|27|37|57)\b", plain, re.I)
    if a:
        return 0, int(a.group(1)), 0, plain.casefold()
    if re.search(r"\bS\s*25\s*FE\b", plain, re.I):
        return 1, 0, 0, plain.casefold()
    if re.search(r"\bS\s*25\s*Edge\b", plain, re.I):
        return 1, 2, 0, plain.casefold()
    if re.search(r"\bS\s*25\s*Ultra\b", plain, re.I):
        return 1, 3, 0, plain.casefold()
    if re.search(r"\bS\s*25\b", plain, re.I):
        return 1, 1, 0, plain.casefold()
    return 9, 0, 0, plain.casefold()


def model_group_key(item):
    """A stable model family key used only for visual spacing in rendered price lists."""
    plain = FLAGS.sub("", normal_title(item.title)).strip()

    model = iphone_model(plain)
    if model:
        return model.casefold()

    watch = re.search(r"\b(?:Series\s*\d{1,2}|SE(?:\s*\d{1,2})?|Ultra(?:\s*\d{1,2})?)\b", plain, re.I)
    if watch and re.search(r"\bWatch\b", plain, re.I):
        return ("apple watch " + watch.group(0)).casefold()

    # Most phone/tablet price rows put RAM/storage immediately after the model.
    storage = re.search(r"\b(?:\d{1,2}/\d{2,4}(?:GB|TB)?|\d{2,4}(?:GB|TB))\b", plain, re.I)
    if storage:
        prefix = plain[:storage.start()].strip(" -—–/·")
        if prefix:
            return clean(prefix).casefold()

    known = [
        r"\b(?:Samsung\s+)?Buds\s*\d+(?:\s*FE)?\b",
        r"\b(?:Samsung\s+)?A\s*\d{2,3}[A-Za-z]*\b",
        r"\b(?:Samsung\s+)?S\s*\d{2}(?:\s*(?:FE|Edge|Ultra|\+|Plus))?\b",
        r"\b(?:Samsung\s+)?Tab\s*S\s*\d+(?:\s*Ultra)?\b",
        r"\b(?:Z\s*)?(?:Fold|Flip)\s*\d+\b",
        r"\b(?:Oura\s+Ring|Oura)\s*\d+\b",
        r"\bCoros\s+Pace\s*\d+\b",
        r"\bDJI\s+Osmo\s+(?:Pocket|Action)\s*\d+\b",
        r"\bInsta\s*360\s*[A-Z]*\d+\b|\bInsta360\s*[A-Z]*\d+\b",
        r"\b(?:Ray-Ban\s+Meta\s+)?(?:Wayfarer|Skyler)\b",
        r"\bR(?:O|Ø)DE\s+Wireless\s+(?:Me|Pro|Micro|GO(?:\s*\d+)?)\b",
        r"\b(?:AirPods|iPad)\s+[A-Za-z]*\s*\d+\b",
    ]
    for pattern in known:
        match = re.search(pattern, plain, re.I)
        if match:
            return clean(match.group(0)).casefold()

    # Generic numbered product families: keep through the first real model number,
    # plus a short modifier such as Pro/Ultra/FE/Edge/S3 when present.
    tokens = plain.split()
    for index, token in enumerate(tokens):
        if not re.search(r"\d", token):
            continue
        if index == 0 and re.fullmatch(r"Insta360", token, re.I):
            continue
        end = index + 1
        while end < len(tokens) and end <= index + 2 and re.fullmatch(
            r"(?:Pro|Max|Ultra|Plus|FE|Edge|Mini|Air|S\d+[A-Za-z]*|Gen\d+)", tokens[end], re.I
        ):
            end += 1
        return clean(" ".join(tokens[:end])).casefold()

    # If there is no reliable model signal, keep rows together rather than
    # inserting random gaps based on colour/accessory wording.
    return item.block.casefold()


def display_title(item):
    """Copyable model attributes, with the SIM kind only when it is known."""
    # Apply at render time so previously saved prices lose the note as well.
    title = normal_title(ORIGINAL_PACKAGING.sub("", item.title))
    if iphone_model(title):
        explicit = sim_type(title)
        if explicit == "unknown" and SIM_LABELS.get(item.sim):
            title += " · " + SIM_LABELS[item.sim]
    return title


def iphone_bundle_key(block):
    """Physical iPhone message requested by the operator."""
    if block == "iPhone Air":
        return "iphone17"
    match = re.fullmatch(r"iPhone\s+(\d{1,2})(?:e)?(?:\s+(?:Plus|Pro(?:\s+Max)?))?", block, re.I)
    if not match:
        return ""
    number = int(match.group(1))
    if 11 <= number <= 15:
        return "iphone11-15"
    if number == 16:
        return "iphone16"
    if number == 17:
        return "iphone17"
    return ""


def iphone_bundle_section_rank(title):
    """Operator-requested visual order inside the large iPhone messages."""
    name = title.casefold().strip()
    if name == "iphone 17e":
        return (0, name)
    if name == "iphone air":
        return (1, name)
    if name == "iphone 17":
        return (2, name)
    if name == "iphone 17 pro":
        return (3, name)
    if name == "iphone 17 pro max":
        return (4, name)
    return (10, name)


def iphone_bundle_header(bundle, titles):
    if bundle == "iphone11-15":
        return "iPhone 11 / 12 / 13 / 14 / 15"
    if bundle == "iphone16":
        base = "iPhone 16 / 16 Plus / 16 Pro / 16 Pro Max"
        if any(title.casefold() == "iphone 16e" for title in titles):
            base += " / 16e"
        return base
    if bundle == "iphone17":
        ordered = sorted(titles, key=iphone_bundle_section_rank)
        labels = []
        for title in ordered:
            name = title.casefold()
            if name == "iphone air":
                labels.append("17 Air")
            elif name.startswith("iphone "):
                labels.append(title[7:])
            else:
                labels.append(title)
        return "iPhone " + " / ".join(labels)
    return " / ".join(titles)


def physical_brand_label(title):
    name = clean(title)
    lower = name.casefold()
    if lower.startswith("iphone"):
        return "iPhone"
    if lower.startswith(("airpods", "apple watch", "ipad", "macbook", "imac", "mac mini", "mac studio", "apple tv", "airtag", "apple")):
        return "Apple"
    if lower.startswith("samsung"):
        return "Samsung"
    if lower.startswith("ray-ban"):
        return "Ray-Ban Meta"
    if lower.startswith("dji") or lower.startswith("insta360"):
        return "DJI / Insta360"
    if lower.startswith("harman") or lower.startswith("bose"):
        return "Harman Kardon / Bose"
    if lower.startswith("kodak") or lower.startswith("fujifilm"):
        return "Kodak / Fujifilm"
    return name


def product_storage_key(item):
    """Storage part for RAM/storage products such as Galaxy S26 12/256 or 16/1TB."""
    title = FLAGS.sub("", clean(item.title))
    match = re.search(r"\b\d{1,2}\s*/\s*(\d{1,4})\s*(GB|TB)?\b", title, re.I)
    if not match:
        return ""
    return match.group(1) + (match.group(2) or "GB").upper()


def iphone_storage_rank(item):
    key = iphone_storage_key(item)
    match = re.fullmatch(r"(\d+)(GB|TB)", key, re.I)
    if not match:
        return 10**9
    value = int(match.group(1))
    if match.group(2).upper() == "TB":
        value *= 1024
    return value


def render_block_lines(block, block_items, settings, overrides, closed=False):
    if closed:
        return ["Продажи закрыты"]

    has_activation = any(activation_state(item.title) != "unknown" or iphone_model(item.title) for item in block_items)

    def condition(item):
        value = activation_state(item.title)
        return "inactive" if value == "unknown" else value

    lines = []
    statuses = sorted({condition(item) for item in block_items}, key=lambda status: CONDITION_ORDER[status])
    for status_index, status in enumerate(statuses):
        status_items = [item for item in block_items if condition(item) == status]
        if status_index and lines and lines[-1] != "":
            lines.append("")
        # Ordinary/non-activated stock is the default and needs no noisy heading.
        # Only explicitly active stock gets its own visible section.
        if has_activation and status == "active":
            lines.append("<b>— Актив —</b>")
            lines.append("")

        last_sim = None
        last_samsung_section = None
        last_model_key = None
        last_storage = None
        sorter = samsung_a_s25_sort if block == "Samsung A + S25" else item_sort

        def full_sort(item):
            if iphone_model(item.title):
                return (SIM_ORDER.get(item.sim, 99), iphone_storage_rank(item), sorter(item))
            return (SIM_ORDER.get(item.sim, 99), 0, sorter(item))

        for item in sorted(status_items, key=full_sort):
            if block == "Samsung A + S25":
                samsung_section = "Galaxy S25" if re.search(r"\bS\s*25\b", item.title, re.I) else "Galaxy A"
                if samsung_section != last_samsung_section:
                    if last_samsung_section is not None and lines and lines[-1] != "":
                        lines.append("")
                    lines.append("<b>— " + samsung_section + " —</b>")
                    lines.append("")
                    last_samsung_section = samsung_section

            if iphone_model(item.title) and item.sim != last_sim:
                label = SIM_LABELS.get(item.sim)
                if label:
                    if lines and lines[-1] != "":
                        lines.append("")
                    lines.append("<b>— " + label + " —</b>")
                    lines.append("")
                last_sim = item.sim

            if iphone_model(item.title):
                storage = iphone_storage_key(item)
                if (storage and last_storage is not None and storage != last_storage
                        and lines and lines[-1] != "" and not lines[-1].startswith("<b>")):
                    lines.append("")
                if storage:
                    last_storage = storage
            else:
                model_key = model_group_key(item)
                model_changed = last_model_key is not None and model_key != last_model_key
                if (model_changed and lines and lines[-1] != "" and not lines[-1].startswith("<b>")):
                    lines.append("")
                if model_changed:
                    last_storage = None
                if block.startswith("Samsung"):
                    storage = product_storage_key(item)
                    if (storage and last_storage is not None and storage != last_storage
                            and lines and lines[-1] != "" and not lines[-1].startswith("<b>")):
                        lines.append("")
                    if storage:
                        last_storage = storage
                last_model_key = model_key

            row = display_title(item) + " — " + price_text(marked_price(item, settings, overrides), item.currency)
            line = "<code>" + html.escape(row) + "</code>"
            if units(line) > 3000:
                raise ValueError("Слишком длинное наименование товара")
            lines.append(line)

    while lines and lines[-1] == "":
        lines.pop()
    return lines


def section_chunks(title, lines, limit=3200):
    """Split a logical block only when Telegram forces us to; never add Part labels."""
    result = []
    chunk = []
    size = 0
    for line in lines:
        line_size = units(line) + 1
        if chunk and size + line_size > limit:
            while chunk and chunk[-1] == "":
                chunk.pop()
            result.append({"title": title, "body": "\n".join(chunk), "part": len(result)})
            chunk = []
            size = 0
        chunk.append(line)
        size += line_size
    if chunk:
        while chunk and chunk[-1] == "":
            chunk.pop()
        result.append({"title": title, "body": "\n".join(chunk), "part": len(result)})
    return result or [{"title": title, "body": "", "part": 0}]


def build_physical_message(sections, bundle=""):
    if not sections:
        return ""
    if len(sections) == 1 and not bundle:
        section = sections[0]
        heading = physical_brand_label(section["title"])
        if heading != section["title"]:
            return (
                "<b>" + html.escape(heading) + "</b>\n\n"
                + "<b>— " + html.escape(section["title"]) + " —</b>\n\n"
                + section["body"]
            )
        return "<b>" + html.escape(section["title"]) + "</b>\n\n" + section["body"]

    titles = []
    for section in sections:
        if section["title"] not in titles:
            titles.append(section["title"])

    if bundle:
        heading = iphone_bundle_header(bundle, titles)
    else:
        brands = []
        for title in titles:
            label = physical_brand_label(title)
            if label not in brands:
                brands.append(label)
        heading = " • ".join(brands)

    bodies = []
    for section in sections:
        bodies.append("<b>— " + html.escape(section["title"]) + " —</b>\n\n" + section["body"])
    # Three newlines = a visible empty line between logical sections.
    return "<b>" + html.escape(heading) + "</b>\n\n" + "\n\n\n".join(bodies)


def split_bundle_sections(sections, bundle, limit=3950):
    batches = []
    current = []
    for section in sections:
        candidate = current + [section]
        if current and units(build_physical_message(candidate, bundle)) > limit:
            batches.append(current)
            current = [section]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def physical_section_family(title):
    """Keep major ecosystems together instead of mixing them with unrelated brands."""
    if title in {"CPO", "ASIS", "Аксессуары"}:
        return "isolated"
    label = physical_brand_label(title)
    if label == "Apple":
        return "apple"
    if label == "Samsung":
        return "samsung"
    return "other"


def pack_physical_sections(sections, limit=3950):
    """Pack by ecosystem: Apple together, Samsung together, other small brands separately."""
    # Saved custom block order may interleave Apple/Samsung with unrelated brands.
    # Compact these ecosystems at their first occurrence before packing so a tiny
    # Apple TV/Mac mini section can never be stranded behind Honor/Oura/etc.
    compacted = []
    emitted_families = set()
    for section in sections:
        if iphone_bundle_key(section["title"]):
            compacted.append(section)
            continue
        family = physical_section_family(section["title"])
        if family in {"apple", "samsung"}:
            if family in emitted_families:
                continue
            emitted_families.add(family)
            compacted.extend([
                entry for entry in sections
                if not iphone_bundle_key(entry["title"]) and physical_section_family(entry["title"]) == family
            ])
        else:
            compacted.append(section)
    sections = compacted

    physical = []
    pending = []
    pending_family = ""
    seen_iphone_bundles = set()

    def flush_pending():
        nonlocal pending, pending_family
        if pending:
            physical.append(("", pending))
            pending = []
            pending_family = ""

    index = 0
    while index < len(sections):
        section = sections[index]
        bundle = iphone_bundle_key(section["title"])
        if bundle:
            flush_pending()
            if bundle not in seen_iphone_bundles:
                seen_iphone_bundles.add(bundle)
                bundle_sections = [entry for entry in sections if iphone_bundle_key(entry["title"]) == bundle]
                if bundle in {"iphone11-15", "iphone17"}:
                    bundle_sections.sort(key=lambda entry: iphone_bundle_section_rank(entry["title"]))
                for batch in split_bundle_sections(bundle_sections, bundle, limit):
                    physical.append((bundle, batch))
            index += 1
            continue

        family = physical_section_family(section["title"])
        if family == "isolated":
            flush_pending()
            physical.append(("", [section]))
            index += 1
            continue

        # Never let Apple/Samsung spill into Honor, Kodak, Marshall, Oura, etc.
        if pending and family != pending_family:
            flush_pending()

        candidate = pending + [section]
        if pending and units(build_physical_message(candidate)) > limit:
            flush_pending()
            pending = [section]
            pending_family = family
        else:
            pending = candidate
            pending_family = family
        index += 1

    flush_pending()
    return physical


def rendered_page_title(content):
    """Visible top heading of one final Telegram price message."""
    match = re.match(r"<b>\s*(?:—\s*)?(.*?)(?:\s*—)?\s*</b>", content or "")
    return html.unescape(match.group(1)).strip() if match else "Прайс"


def render_blocks(items, settings, overrides=None, closed=False):
    """Render logical sections, pack them, then order the real Telegram messages."""
    overrides = overrides or {}
    groups = OrderedDict()
    for item in items:
        groups.setdefault(item.block, []).append(item)

    # Old block_order contained parser-level names such as Mac mini, AirPods or
    # Samsung S26. Those are no longer Telegram messages, so it must not drive
    # publication order. The operator now orders the final physical headings.
    names = ordered_blocks(groups, [])
    logical_sections = []
    for block in names:
        lines = render_block_lines(block, groups[block], settings, overrides, closed=closed)
        chunk_limit = 1800 if physical_section_family(block) == "apple" else 3200
        logical_sections.extend(section_chunks(block, lines, limit=chunk_limit))

    built = []
    for bundle, sections in pack_physical_sections(logical_sections):
        content = build_physical_message(sections, bundle)
        seed = bundle + "|" + "|".join(f'{section["title"]}#{section["part"]}' for section in sections)
        key = hashlib.sha256(seed.encode()).hexdigest()[:16] + ":0"
        built.append((key, content, rendered_page_title(content)))

    preferred = [clean(value) for value in overrides.get("physical_order", []) if clean(value)]
    if preferred:
        rank = {name.casefold(): index for index, name in enumerate(preferred)}
        built = [entry for _, entry in sorted(
            enumerate(built),
            key=lambda pair: (rank.get(pair[1][2].casefold(), len(rank)), pair[0]),
        )]

    pages = OrderedDict()
    for key, content, _title in built:
        pages[key] = content
    return pages
