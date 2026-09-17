"""Control extension for an operator-written pinned first price message."""

from control_group import GroupBindingController


class FirstMessageController(GroupBindingController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.first_message_waiting = set()

    def menu(self):
        menu = super().menu()
        rows = list(menu.get("inline_keyboard", []))
        # Keep group binding last; the custom first-message action is immediately above it.
        bind_row = rows.pop() if rows and rows[-1] and rows[-1][0].get("callback_data") == "bind_group_picker" else None
        rows.append([{"text": "📝 Первое сообщение", "callback_data": "first_message"}])
        if bind_row:
            rows.append(bind_row)
        return {"inline_keyboard": rows}

    async def _prompt_first_message(self, chat_id, user_id):
        self.first_message_waiting.add(user_id)
        current = str(self.service.state.get("first_message_text", "") or "").strip()
        note = ""
        if current:
            note = "\n\nСейчас сохранено:\n" + current[:1200]
        await self.send(
            chat_id,
            "📝 Отправь следующим сообщением текст, который должен стоять первым перед прайсом.\n"
            "Я сохраню его, поставлю перед всеми сообщениями прайса и автоматически закреплю."
            + note,
        )

    async def _apply_first_message(self, chat_id, user_id, text):
        self.first_message_waiting.discard(user_id)
        publisher = self.service.publisher
        if not hasattr(publisher, "set_first_message"):
            await self.send(chat_id, "Эта версия ещё не умеет создавать первое сообщение", self.menu())
            return
        try:
            await publisher.set_first_message(text)
        except Exception as exc:
            await self.send(chat_id, "Не удалось сохранить первое сообщение: " + str(exc), self.menu())
            return

        publish_note = ""
        if self.service.ready:
            try:
                count, changes = await self.service.refresh_format()
                publish_note = f"\nПрайс заново выстроен после него: {count} позиций, изменений: {changes}."
            except Exception:
                publish_note = "\nТекст и закрепление сохранены; прайс достроится при следующем обновлении."
        await self.send(
            chat_id,
            "✅ Первое сообщение сохранено и закреплено. Оно будет первым среди сообщений прайса."
            + publish_note,
            self.menu(),
        )

    async def handle_callback(self, callback):
        data = callback.get("data") or ""
        if data in {"first_message", "sim"} or data.startswith("sim:"):
            user_id = callback.get("from", {}).get("id")
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            chat_type = chat.get("type", "private")
            if not self.allowed(user_id, chat_type):
                await self.answer_callback(callback["id"], f"Нет доступа. Твой ID: {user_id}", True)
                return
            await self.answer_callback(callback["id"])

            if data == "first_message":
                await self._prompt_first_message(chat_id, user_id)
                return

            if data == "sim":
                await self.send(chat_id, "Фильтр iPhone по указанному поставщиком типу SIM. eSIM — только чистая eSIM; SIM + eSIM — отдельный вариант:", {
                    "inline_keyboard": [
                        [{"text": "Все", "callback_data": "sim:all"}, {"text": "SIM", "callback_data": "sim:sim"}],
                        [{"text": "SIM + eSIM", "callback_data": "sim:hybrid"}, {"text": "eSIM", "callback_data": "sim:esim"}],
                        [{"text": "2 SIM", "callback_data": "sim:dual"}, {"text": "Не указан", "callback_data": "sim:unknown"}],
                    ]
                })
                return

            choice = data.split(":", 1)[1]
            if choice not in {"all", "sim", "hybrid", "esim", "dual", "unknown"}:
                await self.send(chat_id, "Неизвестный фильтр SIM", self.menu())
                return
            self.service.set_option("sim_filter", choice)
            try:
                await self.service.refresh_format()
            except Exception as exc:
                await self.send(chat_id, "Фильтр сохранён, но прайс пока не обновился: " + str(exc), self.menu())
                return
            await self.send(chat_id, "Фильтр применён: " + choice, self.menu())
            return

        await super().handle_callback(callback)

    async def handle_message(self, message):
        user_id = message.get("from", {}).get("id")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type", "private")
        text = (message.get("text") or "").strip()

        # Never steal login codes/passwords from the existing login flow.
        if user_id in self.login_flows:
            await super().handle_message(message)
            return

        command = text.split(maxsplit=1)[0].lower().split("@", 1)[0] if text else ""
        if command == "/first":
            if not self.allowed(user_id, chat_type):
                await self.explain_access(chat_id, user_id, chat_type)
                return
            value = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if value:
                await self._apply_first_message(chat_id, user_id, value)
            else:
                await self._prompt_first_message(chat_id, user_id)
            return

        if command == "/cancel" and user_id in self.first_message_waiting:
            self.first_message_waiting.discard(user_id)
            await self.send(chat_id, "Редактирование первого сообщения отменено", self.menu())
            return

        if (
            user_id in self.first_message_waiting
            and chat_type == "private"
            and user_id in self.admins
            and text
            and not command.startswith("/")
        ):
            await self._apply_first_message(chat_id, user_id, text)
            return

        await super().handle_message(message)
