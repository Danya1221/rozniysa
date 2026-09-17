"""Catalog and block-order controls, built on the existing private operator UI."""
import asyncio
import hashlib

from control_first import FirstMessageController
from bot_publisher import is_missing_message_error
from prices import render_blocks, rendered_page_title, select_items


def block_id(block):
    return hashlib.sha256(block.encode()).hexdigest()[:16]


class CatalogController(FirstMessageController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_drafts = {}
        self.order_selected = {}

    def menu(self):
        rows = super().menu()["inline_keyboard"]
        rows.insert(4, [{"text": "↕️ Порядок блоков", "callback_data": "order:show:0"},
                        {"text": "🗂 Каталог", "callback_data": "catalog:refresh"}])
        rows.insert(5, [{"text": "📦 Показывать все позиции", "callback_data": "catalog:all"}])
        return {"inline_keyboard": rows}

    def known_blocks(self):
        """Current physical Telegram message headings, exactly as they are published."""
        catalog = self.service.cached_items(include_closed=True)
        options = self.service.options()
        selected = select_items(catalog, self.service.settings, options)
        pages = render_blocks(selected, self.service.settings, options)
        result = []
        for content in pages.values():
            title = rendered_page_title(content)
            if title and title not in result:
                result.append(title)
        return result

    async def _edit_or_send(self, chat_id, message_id, text, reply_markup=None):
        """Edit the control message in place; only /order without a callback sends one."""
        if not message_id:
            return await self.send(chat_id, text, reply_markup)
        payload = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": str(text),
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            return await self.api("editMessageText", **payload)
        except RuntimeError as exc:
            if "message is not modified" in str(exc).lower():
                return None
            if is_missing_message_error(exc):
                return await self.send(chat_id, text, reply_markup)
            raise

    def _draft_order(self, user_id):
        known = self.known_blocks()
        preferred = self.order_drafts.get(user_id, self.service.options().get("physical_order", []))
        order = [name for name in preferred if name in known]
        order.extend(name for name in known if name not in order)
        self.order_drafts[user_id] = order
        return order

    async def show_order(self, chat_id, user_id, page=0, message_id=None, notice=""):
        order = self._draft_order(user_id)
        selected = self.order_selected.get(user_id)
        if selected not in order:
            selected = None
            self.order_selected.pop(user_id, None)

        rows = []
        buttons = []
        for index, block in enumerate(order):
            buttons.append({
                "text": ("✅ " if block == selected else "") + f"{index + 1}. {block}",
                "callback_data": f"order:select:{block_id(block)}",
            })
        for start in range(0, len(buttons), 2):
            rows.append(buttons[start:start + 2])
        if order:
            rows.append([{"text": "↩️ По умолчанию", "callback_data": "order:reset"}])
        rows.append([{"text": "⬅️ Назад", "callback_data": "order:back"}])

        if order:
            if selected:
                hint = f"\n\nВыбран: {selected}\nОтправь одним сообщением номер позиции от 1 до {len(order)}."
            else:
                hint = "\n\nНажми на нужное сообщение, затем просто отправь номер места."
            text = (
                f"↕️ Порядок сообщений · всего {len(order)}\n"
                "Здесь только реальные сообщения, которые сейчас публикуются в прайсе."
                + hint
            )
        else:
            text = "Сначала запроси прайс — здесь появятся текущие сообщения прайса."
        if notice:
            text = notice + "\n\n" + text
        await self._edit_or_send(chat_id, message_id, text, {"inline_keyboard": rows})

    def _move_selected_to(self, user_id, position):
        order = self._draft_order(user_id)
        selected = self.order_selected.get(user_id)
        if selected not in order:
            raise ValueError("Сначала выбери сообщение из списка")
        if not 1 <= position <= len(order):
            raise ValueError(f"Номер должен быть от 1 до {len(order)}")
        order.remove(selected)
        order.insert(position - 1, selected)
        self.order_drafts[user_id] = order
        return order, selected

    async def _refresh_order_result(self, chat_id, user_id, message_id):
        try:
            count, changes = await self.service.refresh_format()
            await self.show_order(
                chat_id, user_id, message_id=message_id,
                notice=f"✅ Порядок применён. Прайс обновлён: {count} позиций.",
            )
        except Exception as exc:
            await self.show_order(
                chat_id, user_id, message_id=message_id,
                notice="⚠️ Порядок сохранён, но обновление не завершилось: " + str(exc),
            )

    async def refresh_order(self, chat_id, user_id, message_id=None):
        if self.task and not self.task.done():
            await self.show_order(
                chat_id, user_id, message_id=message_id,
                notice="⏳ Обновление уже идёт. Новый порядок сохранён.",
            )
            return
        await self.show_order(chat_id, user_id, message_id=message_id, notice="⏳ Порядок сохранён, обновляю прайс…")
        self.task = asyncio.create_task(self._refresh_order_result(chat_id, user_id, message_id))

    async def _refresh_result(self, chat_id):
        try:
            count, changes = await self.service.refresh_format()
            await self.send(chat_id, f"✅ Порядок и каталог обновлены. В прайсе {count} позиций.", self.menu())
        except Exception as exc:
            await self.send(chat_id, "Настройки сохранены; обновление не завершилось: " + str(exc), self.menu())

    async def refresh_catalog(self, chat_id):
        if self.task and not self.task.done():
            await self.send(chat_id, "Обновление уже идёт. Сохранённые настройки применятся при следующем обновлении.")
            return
        await self.send(chat_id, "Обновляю сообщения и кнопки каталога…")
        self.task = asyncio.create_task(self._refresh_result(chat_id))

    async def handle_callback(self, callback):
        data = callback.get("data") or ""
        if not data.startswith(("order:", "catalog:")):
            await super().handle_callback(callback)
            return
        user_id = callback.get("from", {}).get("id")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if not self.allowed(user_id, chat.get("type")):
            await self.answer_callback(callback["id"], "Нет доступа", True)
            return
        await self.answer_callback(callback["id"])
        try:
            if data.startswith("order:show:"):
                await self.show_order(chat_id, user_id, message_id=message_id)
            elif data.startswith("order:select:"):
                ident = data.rsplit(":", 1)[1]
                order = self._draft_order(user_id)
                block = next((block for block in order if block_id(block) == ident), None)
                if block:
                    self.order_selected[user_id] = block
                    await self.show_order(
                        chat_id, user_id, message_id=message_id,
                        notice=f"Выбран «{block}». Теперь отправь номер его места.",
                    )
                else:
                    await self.show_order(chat_id, user_id, message_id=message_id)
            # Old keyboards may still be visible in Telegram. Open the new screen
            # instead of applying their obsolete logical-block movement actions.
            elif data.startswith(("order:move:", "order:up:", "order:down:")) or data == "order:apply":
                await self.show_order(
                    chat_id, user_id, message_id=message_id,
                    notice="Эта старая кнопка больше не используется. Выбери текущее сообщение и отправь его номер.",
                )
            elif data == "order:reset":
                self.order_drafts.pop(user_id, None)
                self.order_selected.pop(user_id, None)
                self.service.set_option("physical_order", [])
                self.service.set_option("block_order", [])
                await self.refresh_order(chat_id, user_id, message_id)
            elif data == "order:back":
                await self._edit_or_send(chat_id, message_id, "🛠 Управление прайсом", self.menu())
            elif data == "catalog:refresh":
                await self.refresh_catalog(chat_id)
            elif data == "catalog:all":
                options = self.service.options()
                options.update({"sim_filter": "all", "disabled_blocks": [], "include_blocks": [],
                                "exclude_blocks": [], "include_items": [], "exclude_items": [], "allow_accessories": True})
                self.service.state.set("options", options)
                await self.refresh_catalog(chat_id)
        except (ValueError, IndexError) as exc:
            if data.startswith("order:"):
                await self.show_order(chat_id, user_id, message_id=message_id, notice="Не удалось изменить порядок: " + str(exc))
            else:
                await self.send(chat_id, "Не удалось изменить порядок: " + str(exc))

    async def handle_message(self, message):
        text = (message.get("text") or "").strip()
        user_id = message.get("from", {}).get("id")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type", "private")

        # Login codes/passwords and the custom first-message flow always win.
        if user_id in self.login_flows or user_id in getattr(self, "first_message_waiting", set()):
            await super().handle_message(message)
            return

        if user_id in self.order_selected and text and not text.startswith("/"):
            if not self.allowed(user_id, chat_type):
                await self.explain_access(chat_id, user_id, chat_type)
                return
            if not text.isdigit():
                await self.send(chat_id, "Отправь только номер позиции, например 3. Для отмены нажми другой пункт меню.")
                return
            try:
                order, selected = self._move_selected_to(user_id, int(text))
            except ValueError as exc:
                await self.send(chat_id, str(exc))
                return
            self.service.set_option("physical_order", order)
            # Remove the obsolete parser-level order so old Mac mini/AirPods/S26
            # names can never affect the new physical-message layout again.
            self.service.set_option("block_order", [])
            self.order_selected.pop(user_id, None)
            await self.send(chat_id, f"✅ {selected} → место №{int(text)}. Обновляю прайс…")
            await self.refresh_order(chat_id, user_id)
            return

        words = text.split(maxsplit=1)
        command, _, recipient = (words[0].lower() if words else "").partition("@")
        if command != "/order":
            await super().handle_message(message)
            return
        if recipient and self.username and recipient != self.username:
            return
        if not self.allowed(user_id, chat_type):
            await self.explain_access(chat_id, user_id, chat_type)
            return
        if len(words) == 1:
            await self.show_order(chat_id, user_id)
            return

        # Optional direct command: /order Apple 2
        name, separator, raw_position = words[1].rpartition(" ")
        if not separator or not raw_position.isdigit():
            await self.send(chat_id, "Открой /order, выбери текущее сообщение и отправь номер его места.")
            return
        available = self._draft_order(user_id)
        block = next((value for value in available if value.casefold() == name.strip().casefold()), None)
        if not block:
            await self.send(chat_id, "Нет такого текущего сообщения. Открой /order и выбери его кнопкой.")
            return
        self.order_selected[user_id] = block
        try:
            order, selected = self._move_selected_to(user_id, int(raw_position))
        except ValueError as exc:
            await self.send(chat_id, str(exc))
            return
        self.service.set_option("physical_order", order)
        self.service.set_option("block_order", [])
        self.order_selected.pop(user_id, None)
        await self.send(chat_id, f"✅ {selected} → место №{int(raw_position)}. Обновляю прайс…")
        await self.refresh_order(chat_id, user_id)
