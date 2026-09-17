"""Synchronization lifecycle for two supplier prices."""
import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telethon import errors

from prices import Item, render_blocks, select_items
from publisher import Publisher
from supplier import SupplierReader

log = logging.getLogger(__name__)


class LoginRequired(RuntimeError):
    pass


def is_open(settings, now=None):
    """Return whether the fixed daily start hour has been reached.

    There is intentionally no fixed closing hour. Publication closes only
    when every configured supplier explicitly reports closed.
    """
    now = now or datetime.now(timezone.utc)
    hour = now.astimezone(ZoneInfo(settings.timezone)).hour
    return hour >= settings.open_hour


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_lowest(sources):
    """Same full variant across suppliers -> lower purchase price wins."""
    merged = OrderedDict()
    for items in sources:
        for item in items or []:
            current = merged.get(item.key)
            if current is None or item.price < current.price:
                merged[item.key] = item
    return list(merged.values())


class SyncService:
    def __init__(self, client, settings, state):
        self.client = client
        self.settings = settings
        self.state = state
        self.clients = {}
        self.reader_groups = {
            1: [SupplierReader(client, settings, source) for source in settings.sources]
        }
        self.readers = self.reader_groups[1]  # backward-compatible primary readers
        if client is not None:
            self.clients[1] = client
        self.publisher = Publisher(client, settings.target, state, settings)
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.busy = False
        self.last_attempt = 0.0
        self.retry_after = 0.0
        self.ready = client is not None
        self.startup_error = None
        self.account_errors = {}
        self.reload_requested = False

    def _slot_sources(self, slot):
        sources = tuple(self.settings.sources)
        if int(slot) == 1:
            return sources
        # Account 2 exists only to compare the account-specific HI price.
        # Never query the remaining suppliers from the second Telegram account.
        return sources[:1]

    def attach_client(self, client, slot=1):
        slot = int(slot)
        sources = self._slot_sources(slot)
        readers = self.reader_groups.get(slot)
        if readers is None or tuple(reader.source for reader in readers) != tuple(sources):
            readers = [SupplierReader(client, self.settings, source) for source in sources]
            self.reader_groups[slot] = readers
        else:
            for reader in readers:
                reader.client = client
                reader.entity = None

        if client is None:
            self.clients.pop(slot, None)
        else:
            self.clients[slot] = client
            self.account_errors.pop(slot, None)

        if slot == 1:
            self.client = client
            self.readers = readers
            self.publisher.client = client
            self.ready = client is not None
        return readers

    def readers_for_slot(self, slot):
        return self.reader_groups.get(int(slot), [])

    def active_reader_entries(self):
        for slot in sorted(self.clients):
            client = self.clients.get(slot)
            if client is None:
                continue
            for reader in self.reader_groups.get(slot, []):
                yield slot, reader

    def account_configured(self, slot):
        if int(slot) == 1:
            return bool(self.settings.session or self.state.get("session_string", "") or 1 in self.clients)
        return bool(getattr(self.settings, "session_2", "") or self.state.get("session_string_2", "") or 2 in self.clients)

    def startup_status(self):
        return self.startup_error or "Подключение к аккаунту поставщика ещё выполняется"

    def options(self):
        return self.state.get("options", {})

    def enabled(self):
        return self.options().get("enabled", True)

    def set_option(self, key, value):
        options = self.options()
        options[key] = value
        self.state.set("options", options)
        self.wake.set()

    def request_reconnect(self):
        """Reload saved Telegram sessions without restarting the control bot."""
        self.reload_requested = True
        self.wake.set()

    async def connect_account(self, slot):
        slot = int(slot)
        client = self.clients.get(slot)
        if client is None:
            if slot == 1:
                raise LoginRequired("Сессия аккаунта 1 не подключена. Выполни /login 1")
            return False
        try:
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                raise LoginRequired(f"Сессия аккаунта {slot} недействительна. Выполни /login {slot}")
            self.account_errors.pop(slot, None)
            return True
        except (errors.AuthKeyDuplicatedError, errors.UnauthorizedError) as exc:
            message = (
                f"Telegram отозвал сессию аккаунта {slot}. Останови другие копии и выполни /login {slot} заново"
            )
            if slot == 1:
                raise LoginRequired(message) from exc
            self.account_errors[slot] = message
            return False
        except LoginRequired:
            raise
        except Exception as exc:
            if slot == 1:
                raise
            self.account_errors[slot] = f"{type(exc).__name__}: {exc}"
            return False

    async def connect(self):
        if 1 not in self.clients:
            raise LoginRequired("Сессия аккаунта 1 не подключена. Выполни /login 1")
        if not await self.connect_account(1):
            raise LoginRequired("Сессия аккаунта 1 недействительна. Выполни /login 1")
        for slot in sorted(list(self.clients)):
            if slot == 1:
                continue
            ok = await self.connect_account(slot)
            if not ok:
                client = self.clients.get(slot)
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                self.attach_client(None, slot)

    def source_cache_key(self, source, slot=1):
        base = str(source.peer)
        return base if int(slot) == 1 else f"account{int(slot)}:{base}"

    def source_key(self, reader, slot=1):
        return self.source_cache_key(reader.source, slot)

    def source_index(self, source):
        for index, configured in enumerate(self.settings.sources):
            if configured == source:
                return index
        raise ValueError("Неизвестный источник")

    def cached_items(self, include_closed=False):
        cache = self.state.get("sources", {})
        groups = []
        for slot in (1, 2):
            if slot == 2 and not self.account_configured(2):
                continue
            for source in self._slot_sources(slot):
                value = cache.get(self.source_cache_key(source, slot), {})
                if value.get("status") == "closed" and not include_closed:
                    continue
                if value.get("items"):
                    groups.append([Item.from_dict(item) for item in value.get("items", [])])
        return merge_lowest(groups)

    async def render(self, closed=False, items=None):
        options = self.options()
        catalog = self.cached_items(include_closed=closed) if items is None else items
        if closed and not catalog:
            changes = await self.publisher.hide_existing()
            self.state.update({"last_publish": timestamp(), "published_items": 0})
            return 0, changes
        selected = select_items(catalog, self.settings, options)
        pages = render_blocks(selected, self.settings, options, closed=closed)
        if not closed:
            rendered_rows = sum(content.count("<code>") for content in pages.values())
            if rendered_rows != len(selected):
                raise RuntimeError(
                    f"Защита публикации: рендер потерял позиции ({rendered_rows} из {len(selected)}). "
                    "Текущий прайс оставлен без изменений."
                )
        changes = await self.publisher.publish(pages)
        self.state.update({"last_publish": timestamp(), "published_items": 0 if closed else len(selected)})
        return len(selected), changes

    @staticmethod
    def _aggregate_status(values):
        values = list(values)
        if "open" in values:
            return "open"
        if values and all(value == "closed" for value in values):
            return "closed"
        return "error"

    def _both_fresh_open(self, source_statuses):
        return len(self.settings.sources) >= 2 and all(status == "open" for status in source_statuses)

    async def sync(self, force=False):
        async with self.lock:
            if not self.ready:
                return self.startup_status()
            if not self.enabled() and not force:
                return "Синхронизация остановлена"
            if asyncio.get_running_loop().time() < self.retry_after:
                return "Ожидаю завершения FloodWait от Telegram"
            self.busy = True
            try:
                await self.connect()
                self.last_attempt = asyncio.get_running_loop().time()
                self.state.set("last_check", timestamp())

                old_cache = self.state.get("sources", {})
                cache = dict(old_cache)
                errors_found = []
                fresh_by_source = {index: [] for index in range(len(self.settings.sources))}
                fresh_open_groups = []

                for slot, reader in list(self.active_reader_entries()):
                    index = self.source_index(reader.source)
                    key = self.source_key(reader, slot)
                    label = reader.source.label + (f" · аккаунт {slot}" if slot > 1 else "")
                    try:
                        budget = self.settings.response_timeout * (len(reader.source.buttons) + self.settings.catalog_pages + 1) * 2 + 120
                        result = await asyncio.wait_for(reader.fetch(), timeout=budget)
                        previous = cache.get(key, {})
                        status = "closed" if result.closed else "open"
                        fresh_by_source[index].append(status)
                        cache[key] = {
                            "status": status,
                            "checked": timestamp(),
                            "items": previous.get("items", []) if result.closed else [i.to_dict() for i in result.items],
                            "rejected": result.rejected[:200],
                            "rejected_count": len(result.rejected),
                            "error": None,
                            "account": slot,
                        }
                        if status == "open":
                            fresh_open_groups.append(result.items)
                    except (errors.AuthKeyDuplicatedError, errors.UnauthorizedError) as exc:
                        if slot == 1:
                            raise LoginRequired("Сессия Telegram отозвана; выполни /login 1 заново") from exc
                        error = f"Сессия аккаунта {slot} отозвана: {exc}"
                        self.account_errors[slot] = error
                        old = cache.get(key, {})
                        cache[key] = {**old, "error": error, "checked": timestamp(), "account": slot}
                        fresh_by_source[index].append("error")
                        errors_found.append(label + ": " + error)
                    except errors.FloodWaitError as exc:
                        if slot == 1:
                            raise
                        error = f"FloodWait {exc.seconds} сек."
                        old = cache.get(key, {})
                        cache[key] = {**old, "error": error, "checked": timestamp(), "account": slot}
                        fresh_by_source[index].append("error")
                        errors_found.append(label + ": " + error)
                    except Exception as exc:
                        log.warning("Не удалось прочитать %s: %s", label, type(exc).__name__)
                        old = cache.get(key, {})
                        cache[key] = {
                            **old,
                            "error": f"{type(exc).__name__}: {exc}",
                            "checked": timestamp(),
                            "account": slot,
                        }
                        fresh_by_source[index].append("error")
                        errors_found.append(label + ": " + str(exc))

                self.state.set("sources", cache)
                statuses = [self._aggregate_status(fresh_by_source[index])
                            for index in range(len(self.settings.sources))]

                if self.settings.sources and statuses and all(status == "closed" for status in statuses):
                    await self.render(closed=True)
                    message = "Все поставщики закрыты: цены скрыты"
                    self.state.set("last_result", message)
                    return message

                if not fresh_open_groups:
                    message = "Прайс сохранён; нет свежего открытого источника"
                    if errors_found:
                        message += ": " + " | ".join(errors_found)
                    self.state.set("last_result", message)
                    return message

                if not is_open(self.settings) and not self._both_fresh_open(statuses):
                    await self.publisher.hide_existing()
                    message = (
                        f"До {self.settings.open_hour:02d}:00: ждём открытия обоих поставщиков. "
                        "Цены скрыты"
                    )
                    self.state.set("last_result", message)
                    return message

                # Every fresh account/source result participates. Identical variants
                # choose the lowest purchase price before markup.
                catalog = merge_lowest(fresh_open_groups)
                count, changes = await self.render(items=catalog)
                source_note = "/".join(statuses)
                account_note = " + аккаунт 2" if 2 in self.clients else ""
                message = (
                    f"Прайс обновлён: {count} позиций, изменений: {changes}; "
                    f"источники: {source_note}{account_note}"
                )
                self.state.set("last_result", message)
                return message

            except errors.FloodWaitError as exc:
                self.retry_after = asyncio.get_running_loop().time() + exc.seconds + 1
                message = f"Telegram просит подождать {exc.seconds} сек."
                self.state.set("last_result", message)
                return message
            except LoginRequired:
                raise
            except Exception as exc:
                message = f"Ошибка: {type(exc).__name__}: {exc}"
                self.state.set("last_result", message)
                log.exception("Ошибка синхронизации")
                return message
            finally:
                self.busy = False

    async def pause(self):
        async with self.lock:
            self.set_option("enabled", False)

    def _cached_source_statuses(self, cache):
        result = []
        for source in self.settings.sources:
            values = []
            for slot in (1, 2):
                if slot == 2 and not self.account_configured(2):
                    continue
                if source not in self._slot_sources(slot):
                    continue
                value = cache.get(self.source_cache_key(source, slot), {})
                if value.get("status"):
                    values.append(value.get("status"))
            result.append(self._aggregate_status(values))
        return result

    async def refresh_format(self):
        async with self.lock:
            if not self.ready:
                raise RuntimeError(self.startup_status())
            await self.connect()
            cache = self.state.get("sources", {})
            statuses = self._cached_source_statuses(cache)
            closed = bool(statuses) and all(status == "closed" for status in statuses)
            if closed:
                return await self.render(closed=True)
            return await self.render(closed=False)

    def status(self):
        options = self.options()
        cache = self.state.get("sources", {})
        account_1 = "подключён" if 1 in self.clients and self.ready else "не подключён"
        if 2 in self.clients:
            account_2 = "подключён"
        elif self.account_configured(2):
            account_2 = "ошибка: " + self.account_errors.get(2, "ожидает подключения")
        else:
            account_2 = "не подключён"
        lines = [
            "🟢 Синхронизация: включена" if self.enabled() else "⏸ Синхронизация: остановлена",
            f"Telegram: аккаунт 1 — {account_1}; аккаунт 2 — {account_2}",
            f"Интервал: {options.get('poll_seconds', self.settings.poll_seconds) // 60} мин.",
            f"Наценка: {options.get('markup', str(self.settings.markup))} + {options.get('markup_percent', str(self.settings.markup_percent))}%",
            "SIM: " + options.get("sim_filter", self.settings.sim_filter),
            "Последняя проверка: " + self.state.get("last_check", "ещё не было"),
            "Результат: " + self.state.get("last_result", "ожидание"),
        ]
        if not self.ready:
            lines[0] = "⚠️ Чтение прайса недоступно: " + self.startup_status()
        for slot in (1, 2):
            if slot == 2 and not self.account_configured(2):
                continue
            for source in self._slot_sources(slot):
                value = cache.get(self.source_cache_key(source, slot), {})
                status = "ошибка чтения" if value.get("error") else {
                    "open": "открыт", "closed": "закрыт"
                }.get(value.get("status"), "неизвестно")
                suffix = f" · аккаунт {slot}" if slot > 1 else ""
                lines.append(
                    f"{source.label}{suffix}: {status}; получено позиций: {len(value.get('items', []))}; "
                    f"не распознано строк: {value.get('rejected_count', 0)}"
                )
        lines.append(f"Опубликовано позиций: {self.state.get('published_items', 0)}")
        return "\n".join(lines)

    async def disconnect_clients(self):
        for slot, client in list(self.clients.items()):
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            self.attach_client(None, slot)
        self.ready = False

    async def run(self):
        last_start_state = None
        while not self.stop_event.is_set() and not self.reload_requested:
            self.wake.clear()
            now = asyncio.get_running_loop().time()
            start_state = is_open(self.settings)
            interval = self.options().get("poll_seconds", self.settings.poll_seconds)
            due = now - self.last_attempt >= interval or self.last_attempt == 0 or start_state != last_start_state
            if self.enabled() and due and now >= self.retry_after:
                try:
                    await self.sync()
                except LoginRequired as exc:
                    self.startup_error = str(exc)
                    return
                last_start_state = start_state
            if self.reload_requested:
                return
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
