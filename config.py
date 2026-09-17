"""Validated environment settings; importing this module never opens Telegram."""
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def env_value(name, default="", *, aliases=()):
    """Use the canonical nonempty value, then supported legacy names."""
    for candidate in (name, *aliases):
        value = os.getenv(candidate, "").strip()
        if value:
            return value
    return str(default)


def env_int(name, default, minimum=0, maximum=10_000_000, *, aliases=()):
    raw = env_value(name, default, aliases=aliases)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: нужно целое число, получено {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: допустимо от {minimum} до {maximum}")
    return value


def env_float(name, default, minimum=0.1, *, aliases=()):
    try:
        value = float(env_value(name, default, aliases=aliases))
    except ValueError as exc:
        raise ValueError(f"{name}: нужно число") from exc
    if not minimum <= value <= 3600:
        raise ValueError(f"{name}: допустимо от {minimum} до 3600")
    return value


def env_bool(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(f"{name}: укажи true или false")
    return value in {"1", "true", "yes", "on"}


def decimal_value(value):
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Наценка должна быть числом") from exc
    if not result.is_finite() or abs(result) > 10_000_000:
        raise ValueError("Недопустимая наценка")
    return result


def peer(value):
    value = str(value).strip()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    match = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/?", value)
    return "@" + match[1] if match else value


def csv_env(name, *, aliases=()):
    return tuple(x.strip() for x in env_value(name, aliases=aliases).split(",") if x.strip())


@dataclass(frozen=True)
class Source:
    peer: object
    mode: str = "bot"
    request: str = "/start"
    buttons: tuple = ()
    label: str = "Поставщик 1"
    closed_text: str = ""


@dataclass
class Settings:
    api_id: int = 0
    api_hash: str = ""
    session: str = ""
    session_2: str = ""
    target: object = ""
    sources: tuple = ()
    poll_seconds: int = 600
    action_delay: float = 1.0
    response_timeout: float = 45.0
    quiet_seconds: float = 3.0
    history_limit: int = 300
    feed_history_limit: int = 0
    catalog_pages: int = 200
    header: str = "📦 АКТУАЛЬНЫЙ ПРАЙС"
    state_file: str = "state.json"
    markup: Decimal = Decimal("0")
    markup_percent: Decimal = Decimal("0")
    currency: str = "RUB"
    sim_filter: str = "all"
    include_blocks: tuple = ()
    exclude_blocks: tuple = ()
    include_items: tuple = ()
    exclude_items: tuple = ()
    allow_accessories: bool = True
    off_hours: bool = True
    timezone: str = "Europe/Moscow"
    open_hour: int = 10
    close_hour: int = 20
    send_delay: float = 1.0
    bot_token: str = field(default="", repr=False)
    admin_ids: tuple = ()

    @classmethod
    def from_env(cls, *, require_sync=True):
        sources = []
        for suffix in ("", "_2"):
            source_peer = os.getenv("SUPPLIER_BOT" + suffix, "").strip()
            if not source_peer:
                continue
            explicit_request = os.getenv("REQUEST_TEXT" + suffix, "").strip()
            button_path = tuple(x.strip() for x in os.getenv("BUTTON_PATH" + suffix, "").split(">") if x.strip())
            # A configured request/button path expresses intent to query a bot.
            # An explicitly configured feed still never receives commands.
            default_mode = "bot" if not suffix or explicit_request or button_path else "feed"
            mode = env_value("SOURCE_MODE" + suffix, default_mode).lower()
            if mode not in {"bot", "feed"}:
                raise ValueError("SOURCE_MODE: допустимо bot или feed")
            sources.append(Source(
                peer(source_peer), mode,
                os.getenv("REQUEST_TEXT" + suffix, "/start").strip(),
                button_path,
                "Поставщик 2" if suffix else "Поставщик 1",
                closed_text=env_value("CLOSED_TEXT_2" if suffix else "CLOSED_TEXT_1"),
            ))
        root = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        settings = cls(
            api_id=env_int("API_ID", 0, maximum=2**31-1),
            api_hash=os.getenv("API_HASH", "").strip(),
            session=os.getenv("SESSION_STRING", "").strip(),
            session_2=os.getenv("SESSION_STRING_2", "").strip(),
            target=peer(os.getenv("TARGET_CHANNEL", "")),
            sources=tuple(sources),
            poll_seconds=env_int("POLL_SECONDS", 600, 30, 86400),
            action_delay=env_float("AFTER_ACTION_DELAY", 1),
            response_timeout=env_float("RESPONSE_TIMEOUT", 45),
            quiet_seconds=env_float("PRICE_SETTLE_SECONDS", 3, aliases=("SUPPLIER_QUIET_SECONDS",)),
            history_limit=env_int("HISTORY_LIMIT", 300, 10, 3000),
            feed_history_limit=env_int("FEED_HISTORY_LIMIT", 0, 0, 1000000),
            catalog_pages=env_int("MAX_CATALOG_PAGES", 200, 1, 1000),
            header=os.getenv("PRICE_HEADER", "📦 АКТУАЛЬНЫЙ ПРАЙС").strip() or "📦 АКТУАЛЬНЫЙ ПРАЙС",
            state_file=os.getenv("STATE_FILE", str(Path(root) / "state.json") if root else "state.json").strip(),
            markup=decimal_value(os.getenv("MARKUP", "0")),
            markup_percent=decimal_value(os.getenv("MARKUP_PERCENT", "0")),
            currency=os.getenv("PRICE_CURRENCY", "RUB").strip().upper(),
            sim_filter=os.getenv("SIM_FILTER", "all").strip().lower(),
            include_blocks=csv_env("INCLUDE_BLOCKS"), exclude_blocks=csv_env("EXCLUDE_BLOCKS"),
            include_items=csv_env("INCLUDE_ITEMS"), exclude_items=csv_env("EXCLUDE_ITEMS"),
            allow_accessories=env_bool("ALLOW_ACCESSORIES", True),
            off_hours=env_bool("OFF_HOURS_ENABLED", True),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
            open_hour=env_int("OPEN_HOUR", 10, 0, 23, aliases=("WORK_START_HOUR",)),
            close_hour=env_int("CLOSE_HOUR", 20, 0, 23, aliases=("WORK_END_HOUR",)),
            send_delay=env_float("SEND_DELAY", 1),
            bot_token=env_value("BOT_TOKEN", aliases=("CONTROL_BOT_TOKEN",)),
            admin_ids=tuple(int(x) for x in csv_env("ADMIN_IDS", aliases=("ADMIN_ID",))),
        )
        settings.validate(require_sync=require_sync)
        return settings

    def validate(self, *, require_sync=True):
        required = [("API_ID", self.api_id), ("API_HASH", self.api_hash)]
        if require_sync:
            required += [("SESSION_STRING", self.session), ("SUPPLIER_BOT", self.sources),
                         ("TARGET_CHANNEL", self.target)]
        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError("Не заполнены переменные: " + ", ".join(missing))
        if any(value <= 0 for value in self.admin_ids):
            raise ValueError("ADMIN_IDS: нужны положительные ID пользователей, а не ID группы")
        if self.markup_percent <= -100:
            raise ValueError("MARKUP_PERCENT должен быть больше -100")
        if self.sim_filter not in {"all", "sim", "esim", "hybrid", "dual", "unknown"}:
            raise ValueError("SIM_FILTER: all, sim, esim, hybrid, dual или unknown")
        if self.currency not in {"RUB", "USD", "EUR"}:
            raise ValueError("PRICE_CURRENCY: RUB, USD или EUR")
        if self.response_timeout <= self.quiet_seconds:
            raise ValueError("RESPONSE_TIMEOUT должен быть больше PRICE_SETTLE_SECONDS")
        if self.off_hours and self.open_hour == self.close_hour:
            raise ValueError("OPEN_HOUR и CLOSE_HOUR должны отличаться")
        if len(self.header) > 120 or "\n" in self.header:
            raise ValueError("PRICE_HEADER: одна строка до 120 символов")
        if not self.state_file:
            raise ValueError("STATE_FILE не может быть пустым")
        ZoneInfo(self.timezone)
