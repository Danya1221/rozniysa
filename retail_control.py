"""Private administrator controls for covers and recoverable publication."""
from control_catalog import CatalogController, block_id
from prices import select_items
from retail_catalog import to_product, render_prices


class RetailController(CatalogController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cover_waiting = {}
        self.retry_waiting = set()

    def menu(self):
        rows = super().menu()["inline_keyboard"]
        recovery = self.service.state.get("retail_pending_recovery", {}) or {}
        if recovery and recovery.get("binding") == self.service.publisher.binding():
            rows.insert(0, [{"text": "♻️ Восстановить публикацию", "callback_data": "retail:recover_publish"}])
        rows.insert(0, [{"text": "📷 Обложки брендов", "callback_data": "retail:covers"}])
        return {"inline_keyboard": rows}

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
        elif data == "retail:recover_publish":
            recovery = self.service.state.get("retail_pending_recovery", {}) or {}
            if not recovery or recovery.get("binding") != self.service.publisher.binding():
                await self.send(user_id, "Зависшей отправки уже нет. Можно запросить прайс ещё раз.", self.menu())
                return
            if self.service.lock.locked() or (self.task and not self.task.done()):
                await self.send(user_id, "Сначала дождись завершения текущего обновления.")
                return
            field = recovery.get("field")
            if field not in {"pending_publish", "pending_retail_node"}:
                await self.send(user_id, "Не удалось определить зависшую отправку. Используй /status.")
                return
            # This is deliberately user-confirmed: pressing the button means the
            # previewed message was checked in the group and is NOT present there.
            self.service.state.update({field: None, "retail_pending_recovery": None})
            await self.send(user_id, "♻️ Восстанавливаю публикацию…")
            await self.refresh_catalog(user_id)
        elif data == "retail:retry" and user_id in self.retry_waiting:
            if self.service.lock.locked() or (self.task and not self.task.done()):
                await self.send(user_id, "Сначала дождись завершения текущего обновления.")
                return
            self.retry_waiting.discard(user_id)
            self.service.state.update({"pending_publish": None, "pending_retail_node": None,
                                       "retail_pending_recovery": None})
            await self.refresh_catalog(user_id)

    async def background_sync(self, chat_id):
        try:
            result = await self.service.sync(force=True)
            recovery = self.service.state.get("retail_pending_recovery", {}) or {}
            if recovery and recovery.get("binding") == self.service.publisher.binding():
                preview = recovery.get("preview") or "(фрагмент недоступен)"
                text = (
                    result
                    + "\n\n⚠️ Есть незавершённая отправка. Проверь в группе, есть ли сообщение:\n\n"
                    + "«" + preview + "»\n\n"
                    + "Если такого сообщения НЕТ — нажми «Восстановить публикацию». "
                      "Если оно есть, кнопку не нажимай: так мы не создадим дубль."
                )
                await self.send(chat_id, text, {"inline_keyboard": [[
                    {"text": "♻️ Восстановить публикацию", "callback_data": "retail:recover_publish"}
                ]]})
                return
            await self.send(chat_id, result, self.menu())
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Ошибка ручного обновления розницы")
            await self.send(chat_id, "Ошибка обновления. Проверь /status и журнал Railway", self.menu())

    async def handle_message(self, message):
        user_id = message.get("from", {}).get("id")
        chat = message.get("chat", {})
        if user_id in self.login_flows:
            return await super().handle_message(message)
        text = (message.get("text") or "").strip()
        if self.allowed(user_id, chat.get("type")) and chat.get("id") == user_id:
            if text == "/retry_publish":
                self.retry_waiting.add(user_id)
                await self.send(user_id, "Проверь последние сообщения группы. Нажимай только если последняя отправка "
                                "точно НЕ появилась: повтор может создать дубль.", {"inline_keyboard": [[
                                    {"text": "Проверил: сообщения нет, повторить", "callback_data": "retail:retry"}]]})
                return
            if text == "/cancel":
                self.cover_waiting.pop(user_id, None)
                self.retry_waiting.discard(user_id)
            elif user_id in self.cover_waiting and message.get("photo"):
                brand = self.cover_waiting.pop(user_id)
                covers = self.service.state.get("retail_covers", {})
                covers[brand] = message["photo"][-1]["file_id"]
                self.service.state.set("retail_covers", covers)
                await self.refresh_catalog(user_id)
                return
        await super().handle_message(message)
