"""Control extension for binding the finished price to a Telegram group.

Preferred flow: the operator forwards a message from the target group to the control
bot. If Telegram exposes the source chat, it is bound immediately. Because Telegram
may hide the source group for ordinary user-authored forwards, the same screen also
offers Telegram's native chat picker as a reliable one-tap fallback.
"""

from control_botapi import BotAPIController


PUBLISH_CHAT_TYPES = {"group", "supergroup", "channel"}


class GroupBindingController(BotAPIController):
    def menu(self):
        menu = super().menu()
        rows = list(menu.get("inline_keyboard", []))
        rows.append([{"text": "🔗 Привязать группу", "callback_data": "bind_group_picker"}])
        return {"inline_keyboard": rows}

    def _forwarded_chat(self, message):
        """Return the source group/channel when Telegram exposes it in a forward."""
        candidates = []

        # Bot API 7+: MessageOriginChannel / MessageOriginChat.
        origin = message.get("forward_origin") or {}
        for key in ("chat", "sender_chat"):
            value = origin.get(key)
            if isinstance(value, dict):
                candidates.append(value)

        # Older Bot API compatibility.
        legacy = message.get("forward_from_chat")
        if isinstance(legacy, dict):
            candidates.append(legacy)

        for chat in candidates:
            if chat.get("type") in PUBLISH_CHAT_TYPES and chat.get("id") is not None:
                return chat
        return None

    async def _bind_chat(self, reply_chat_id, source_chat):
        publisher = self.service.publisher
        if not hasattr(publisher, "bind_group"):
            await self.send(reply_chat_id, "Эта версия ещё не умеет привязывать группу")
            return False

        source_id = int(source_chat["id"])
        title = source_chat.get("title") or source_chat.get("username") or str(source_id)
        source_type = source_chat.get("type") or "group"

        try:
            await publisher.bind_group(source_id, title=title, chat_type=source_type)
        except Exception as exc:
            await self.send(
                reply_chat_id,
                "Не удалось привязать эту группу. Убедись, что @"
                + (self.username or "управляющий_бот")
                + " добавлен в неё и может писать.\n\nОшибка: " + str(exc),
            )
            return False

        self.service.state.set("last_result", f"Группа публикации привязана: {title}")

        # If prices are already cached, publish them immediately to the newly bound
        # group. A failure here must not undo the binding itself.
        publish_note = ""
        if self.service.ready:
            try:
                count, changes = await self.service.refresh_format()
                publish_note = f"\nПрайс сразу обновлён: {count} позиций, изменений: {changes}."
            except Exception as exc:
                publish_note = "\nГруппа сохранена. Прайс можно отправить кнопкой «🔄 Запросить сейчас»."

        await self.send(
            reply_chat_id,
            f"✅ Группа привязана: {title}.\nТеперь прайс будет публиковаться только туда.{publish_note}",
            self.menu(),
        )
        return True

    async def _send_group_picker(self, chat_id):
        # Telegram native chat picker. bot_is_member=True prevents selecting a group
        # where the control bot is absent, which removes another common dead end.
        markup = {
            "keyboard": [[{
                "text": "📎 Выбрать группу",
                "request_chat": {
                    "request_id": 731,
                    "chat_is_channel": False,
                    "bot_is_member": True,
                    "request_title": True,
                },
            }]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
        await self.send(
            chat_id,
            "Перешли сюда любое сообщение из нужной группы — я попробую привязать её автоматически.\n\n"
            "Если Telegram скроет источник пересылки, нажми «📎 Выбрать группу» ниже — это привяжет её без ID и без /bind.",
            markup,
        )

    async def handle_callback(self, callback):
        data = callback.get("data") or ""
        if data == "bind_group_picker":
            user_id = callback.get("from", {}).get("id")
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            chat_type = chat.get("type", "private")
            if not self.allowed(user_id, chat_type):
                await self.answer_callback(callback["id"], f"Нет доступа. Твой ID: {user_id}", True)
                return
            await self.answer_callback(callback["id"])
            await self._send_group_picker(chat_id)
            return
        await super().handle_callback(callback)

    async def handle_message(self, message):
        user_id = message.get("from", {}).get("id")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type", "private")

        # Reliable native picker result in the private control chat.
        shared = message.get("chat_shared")
        if shared and chat_type == "private":
            if user_id not in self.admins:
                await self.explain_access(chat_id, user_id, chat_type)
                return
            source = {
                "id": shared.get("chat_id"),
                "type": "group",
                "title": shared.get("title") or "",
            }
            if source["id"] is not None:
                await self._bind_chat(chat_id, source)
                return

        # Preferred UX requested by the operator: just forward a message from the
        # target group into the private control bot.
        has_forward = bool(message.get("forward_origin") or message.get("forward_from_chat"))
        if has_forward and chat_type == "private":
            if user_id not in self.admins:
                await self.explain_access(chat_id, user_id, chat_type)
                return
            source = self._forwarded_chat(message)
            if source is not None:
                await self._bind_chat(chat_id, source)
                return

            # Ordinary group forwards may expose only the original person, not the
            # source group. Do not pretend we can infer an ID that Telegram omitted.
            await self._send_group_picker(chat_id)
            return

        text = (message.get("text") or "").strip()
        if text:
            command = text.split(maxsplit=1)[0].lower().split("@", 1)[0]

            # Keep /bind as a compatibility path, but normal setup no longer needs it.
            if command in {"/bind", "/group"}:
                if user_id not in self.admins:
                    await self.send(chat_id, f"Нет доступа. Твой Telegram ID: {user_id}")
                    return

                if chat_type in {"group", "supergroup"}:
                    await self._bind_chat(chat_id, chat)
                    return

                await self._send_group_picker(chat_id)
                return

        await super().handle_message(message)
