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
        if data == "retail:covers":
            brands = list(self.service.publisher.navigation)
            rows = [[{"text": b, "callback_data": "retail:cover:" + block_id(b)}] for b in brands]
            await self.send(user_id, "Выбери бренд и отправь фотографию. /cancel — отмена."
                            if brands else "Сначала запроси прайс: появятся бренды.", {"inline_keyboard": rows})
        elif data.startswith("retail:cover:"):
            brand = next((b for b in self.service.publisher.navigation if block_id(b) == data.rsplit(":", 1)[1]), None)
            if brand:
                self.cover_waiting[user_id] = brand
                await self.send(user_id, f"Отправь фото для «{brand}» как фотографию (не файл). /cancel — отмена.")
        elif data == "retail:retry" and user_id in self.retry_waiting:
            self.retry_waiting.discard(user_id)
            self.service.state.update({"pending_publish": None, "pending_retail_node": None})
            await self.refresh_catalog(user_id)

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
