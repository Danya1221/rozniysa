"""Private administrator controls for covers and recoverable publication."""
import json
from bot_publisher import digest, plain
from control_catalog import CatalogController, block_id
from prices import select_items
from retail_catalog import to_product, render_prices


class RetailController(CatalogController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cover_waiting = {}
        self.retry_waiting = {}

    def menu(self):
        rows = super().menu()["inline_keyboard"]
        rows.insert(0, [{"text": "📷 Обложки брендов", "callback_data": "retail:covers"}])
        if self.pending_snapshot():
            rows.insert(0, [{"text": "♻️ Восстановить публикацию", "callback_data": "retail:recover"}])
        return {"inline_keyboard": rows}

    def pending_snapshot(self):
        binding = self.service.publisher.binding()
        return {field: value for field in ("pending_publish", "pending_retail_node")
                if (value := self.service.state.get(field)) and value.get("binding") == binding}

    async def show_recovery(self, user_id):
        pending = self.pending_snapshot()
        if not pending:
            await self.send(user_id, "Зависших отправок нет. Нажми «Запросить сейчас».", self.menu())
            return
        token = digest(json.dumps(pending, sort_keys=True, ensure_ascii=False))[:16]
        self.retry_waiting[user_id] = (token, pending)
        previews = "\n\n".join(plain(p["text"])[:700] for p in pending.values())
        await self.send(user_id,
            "Группа привязана. Нужно проверить, появилось ли это сообщение после сбоя:\n\n"
            + previews + "\n\nЕсли оно есть — добавь в группу Telegram-аккаунт 1 из /login 1 и нажми «Запросить сейчас». "
            "Бот найдёт сообщение и продолжит обновление.\n"
            "Если ни одного из показанных сообщений нет, нажми кнопку ниже.",
            {"inline_keyboard": [[{"text": "Проверил: сообщений нет — повторить", "callback_data": "retail:retry:" + token}]]})

    def known_blocks(self):
        items = select_items(self.service.cached_items(include_closed=True), self.service.settings, self.service.options())
        _, nav = render_prices([to_product(i, self.service.settings, self.service.options()) for i in items],
                               self.service.order_username)
        return list(dict.fromkeys(s["section"] for sections in nav.values() for s in sections))

    async def handle_callback(self, callback):
        data = callback.get("data", "")
        if not data.startswith("retail:"):
            return await super().handle_callback(callback)
        user_id = callback.get("from", {}).get("id")
        chat = (callback.get("message") or {}).get("chat", {})
        if not self.allowed(user_id, chat.get("type")) or chat.get("id") != user_id:
            return await self.answer_callback(callback["id"], "Нет доступа", True)
        await self.answer_callback(callback["id"])
        if data in {"retail:test_order", "retail:test_remove"}:
            await self.send(user_id, "Тест завершён. В прайс выгружаются только товары поставщиков.", self.menu())
        elif data == "retail:covers":
            brands = list(self.service.publisher.navigation)
            rows = [[{"text": b, "callback_data": "retail:cover:" + block_id(b)}] for b in brands]
            await self.send(user_id, "Выбери бренд и отправь фотографию. /cancel — отмена."
                            if brands else "Сначала запроси прайс: появятся бренды.", {"inline_keyboard": rows})
        elif data.startswith("retail:cover:"):
            brand = next((b for b in self.service.publisher.navigation if block_id(b) == data.rsplit(":", 1)[1]), None)
            if brand:
                self.cover_waiting[user_id] = brand
                await self.send(user_id, f"Отправь фото для «{brand}» как фотографию (не файл). /cancel — отмена.")
        elif data in {"retail:recover", "retail:retry"}:
            await self.show_recovery(user_id)
        elif data.startswith("retail:retry:"):
            if self.service.lock.locked() or (self.task and not self.task.done()):
                await self.send(user_id, "Сначала дождись завершения текущего обновления.")
                return
            current = self.pending_snapshot()
            expected = self.retry_waiting.get(user_id)
            if not expected or expected[0] != data.rsplit(":", 1)[1] or expected[1] != current:
                await self.send(user_id, "Состояние отправки изменилось. Проверь актуальное сообщение.")
                await self.show_recovery(user_id)
                return
            self.retry_waiting.pop(user_id, None)
            self.service.state.update({field: None for field in current})
            await self.refresh_catalog(user_id)

    async def handle_message(self, message):
        user_id = message.get("from", {}).get("id")
        chat = message.get("chat", {})
        if user_id in self.login_flows:
            return await super().handle_message(message)
        text = (message.get("text") or "").strip()
        if self.allowed(user_id, chat.get("type")) and chat.get("id") == user_id:
            if text == "/retry_publish":
                await self.show_recovery(user_id)
                return
            if text == "/cancel":
                self.cover_waiting.pop(user_id, None)
                self.retry_waiting.pop(user_id, None)
            elif user_id in self.cover_waiting and message.get("photo"):
                brand = self.cover_waiting.pop(user_id)
                covers = self.service.state.get("retail_covers", {})
                covers[brand] = message["photo"][-1]["file_id"]
                self.service.state.set("retail_covers", covers)
                await self.refresh_catalog(user_id)
                return
        await super().handle_message(message)
