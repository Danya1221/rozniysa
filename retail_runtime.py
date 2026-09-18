"""Retail synchronization: cached prices remain orderable, availability is explicit."""
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import errors

from prices import Item, select_items
from retail_catalog import to_product, dedupe_products, render_prices
from retail_store import stable_id
from runtime import SyncService, LoginRequired, merge_lowest, timestamp

log = logging.getLogger(__name__)
RETAIL_BUILD = "retail-2026.09.18-1935-test"
RETAIL_TEST_PRODUCT = {
    "id": stable_id("temporary-retail-test|iphone-17|256gb|blue|hybrid"),
    "title": "iPhone 17 256GB Blue Sim+eSim",
    "price": "150000",
    "currency": "RUB",
    "brand": "Apple",
    "section": "iPhone 17",
    "model": "iPhone 17",
    "sim": "hybrid",
    "condition": "inactive",
    "storage_rank": 256,
}


class RetailSyncService(SyncService):
    def __init__(self, client, settings, state, retail, order_username):
        super().__init__(client, settings, state)
        self.retail, self.order_username = retail, order_username

    def status(self):
        text = super().status()
        if hasattr(self.retail, "local"):
            bridge = self.retail.local.get("bridge", "status", {})
            text += "\nСвязь с оформлением: " + bridge.get("message", "ожидает передачи каталога")
        text += "\nСборка: " + RETAIL_BUILD
        return text

    async def connect_account(self, slot):
        try:
            return await super().connect_account(slot)
        except LoginRequired as exc:
            if slot == 1:
                raise
            self.account_errors[slot] = str(exc)
            return False

    def freshness(self):
        cache = self.state.get("sources", {})
        checks, confirmed = [], bool(self.settings.sources)
        for slot in (1, 2):
            if slot == 2 and not self.account_configured(2):
                continue
            for source in self._slot_sources(slot):
                value = cache.get(self.source_cache_key(source, slot), {})
                confirmed = confirmed and value.get("status") == "open" and not value.get("error")
                try:
                    checks.append(datetime.fromisoformat(value["checked"]).timestamp())
                except (KeyError, ValueError, TypeError):
                    checks.append(1)
                    confirmed = False
        hour = datetime.now(ZoneInfo(self.settings.timezone)).hour
        start, end = self.settings.open_hour, self.settings.close_hour
        business_hours = (start <= hour < end) if start < end else (hour >= start or hour < end)
        confirmed = confirmed and business_hours
        checked = min(checks, default=1)
        return bool(confirmed and time.time() - checked < max(1800, self.settings.poll_seconds * 2)), checked

    def retail_cached_items(self):
        """Exclude explicitly closed suppliers while at least one source is open.

        Transient read failures retain their last open snapshot (unconfirmed), as
        before. If every source is closed/unavailable, keep the last complete
        catalog as an unconfirmed fallback for the customer/manager.
        """
        cache = self.state.get("sources", {})
        open_groups = []
        fallback_groups = []
        for slot in (1, 2):
            if slot == 2 and not self.account_configured(2):
                continue
            for source in self._slot_sources(slot):
                value = cache.get(self.source_cache_key(source, slot), {})
                raw_items = value.get("items") or []
                if not raw_items:
                    continue
                group = [Item.from_dict(item) for item in raw_items]
                fallback_groups.append(group)
                # Explicitly closed suppliers are excluded while any supplier is
                # still open. A transient read error keeps its last open snapshot,
                # but freshness() marks the resulting catalog unconfirmed.
                if value.get("status") == "open":
                    open_groups.append(group)
        return merge_lowest(open_groups if open_groups else fallback_groups)

    async def render(self, closed=False, items=None):
        selected = select_items(self.retail_cached_items() if items is None else items,
                                self.settings, self.options())
        products = dedupe_products([to_product(item, self.settings, self.options()) for item in selected])
        pages, navigation = render_prices(products, self.order_username, self.options().get("physical_order", []))

        # Temporary end-to-end checkout probe requested by the owner. Keep it in a
        # completely separate Telegram message, but send the same product to zayavki
        # so tapping the row exercises the real retail -> checkout flow.
        test_product = dict(RETAIL_TEST_PRODUCT)
        test_url = f'https://t.me/{self.order_username}?start=p_{test_product["id"]}'
        pages["__checkout_test__"] = (
            "<b>ТЕСТОВАЯ ПОЗИЦИЯ</b>\n\n"
            f'<a href="{test_url}">iPhone 17 256GB Blue Sim+eSim — 150 000</a>'
        )
        catalog_products = dedupe_products(products + [test_product])

        if sum(content.count('<a href=') for content in pages.values()) != len(catalog_products):
            raise RuntimeError("Количество товаров в базе и сообщениях не совпало; публикация остановлена")
        confirmed, checked = self.freshness()
        await asyncio.to_thread(self.retail.put_catalog, catalog_products, confirmed=confirmed and not closed, checked_at=checked)
        self.publisher.navigation = navigation
        changes = await self.publisher.publish(pages)
        self.state.update({"last_publish": timestamp(), "published_items": len(catalog_products)})
        return len(catalog_products), changes

    async def pause(self):
        self.set_option("enabled", False)
        await asyncio.to_thread(self.retail.mark_uncertain, "Обновление прайса приостановлено")

    async def refresh_format(self):
        if self.lock.locked():
            raise RuntimeError("Обновление уже идёт. Настройки сохранены и применятся после чтения прайса")
        async with self.lock:
            if not self.state.get("sources"):
                raise RuntimeError("Сначала подключи аккаунт и запроси прайс")
            return await self.render(closed=not self.enabled())

    async def sync(self, force=False):
        async with self.lock:
            if not self.ready:
                await asyncio.to_thread(self.retail.mark_uncertain, "Аккаунт поставщика не подключён")
                return self.startup_status()
            if not self.enabled() and not force:
                return "Синхронизация остановлена"
            if asyncio.get_running_loop().time() < self.retry_after:
                return "Ожидаю завершения FloodWait от Telegram"
            self.busy = True
            self.last_attempt = asyncio.get_running_loop().time()
            try:
                await self.connect()
                self.state.set("last_check", timestamp())
                cache = self.state.get("sources", {})
                failures = []
                for slot, reader in list(self.active_reader_entries()):
                    key = self.source_key(reader, slot)
                    previous = cache.get(key, {})
                    try:
                        budget = min(1800, self.settings.response_timeout *
                                     (len(reader.source.buttons) + self.settings.catalog_pages + 1) * 2 + 120)
                        result = await asyncio.wait_for(reader.fetch(), budget)
                        cache[key] = {"status": "closed" if result.closed else "open", "checked": timestamp(),
                                      "items": previous.get("items", []) if result.closed else [i.to_dict() for i in result.items],
                                      "error": None, "account": slot, "rejected": result.rejected[:200],
                                      "rejected_count": len(result.rejected)}
                    except (errors.AuthKeyDuplicatedError, errors.UnauthorizedError) as exc:
                        if slot == 1:
                            raise LoginRequired("Подключи аккаунт заново через /login 1") from exc
                        failures.append(reader.source.label)
                        cache[key] = {**previous, "error": "Сессия отозвана", "account": slot}
                    except errors.FloodWaitError as exc:
                        self.retry_after = asyncio.get_running_loop().time() + exc.seconds + 1
                        cache[key] = {**previous, "error": f"FloodWait {exc.seconds} сек.", "account": slot}
                        failures.append(reader.source.label)
                        break
                    except Exception as exc:
                        failures.append(reader.source.label)
                        cache[key] = {**previous, "error": type(exc).__name__, "account": slot}
                        log.warning("Чтение %s не завершено: %s", reader.source.label, type(exc).__name__)
                    # Checkpoint each source; failure of another source must not erase it.
                    self.state.set("sources", cache)
                self.state.set("sources", cache)
                if not self.enabled() and not force:
                    await asyncio.to_thread(self.retail.mark_uncertain, "Обновление приостановлено")
                    return "Чтение завершено; публикация приостановлена"
                count, changes = await self.render()
                confirmed, _ = self.freshness()
                message = f"Розничный прайс: {count} позиций, изменений: {changes}."
                if not confirmed:
                    message += " Наличие и цену подтверждает менеджер; заявки принимаются."
                if failures:
                    message += " Сбой чтения: " + ", ".join(failures)
                self.state.set("last_result", message)
                return message
            except LoginRequired:
                await asyncio.to_thread(self.retail.mark_uncertain, "Нужен повторный вход к поставщику")
                raise
            except Exception as exc:
                await asyncio.to_thread(self.retail.mark_uncertain, "Обновление не завершено")
                message = "Обновление не завершено: " + str(exc)
                self.state.set("last_result", message)
                log.warning("Синхронизация: %s", type(exc).__name__)
                return message
            finally:
                self.busy = False
