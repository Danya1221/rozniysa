"""Pinned operator-written first message for the published price."""
import asyncio
from contextlib import suppress

from bot_publisher import BotAPIPublisher, digest, is_missing_message_error


class PinnedBotAPIPublisher(BotAPIPublisher):
    """Keep one operator-written message before all managed price posts and pin it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.first_message_lock = asyncio.Lock()

    async def bind_group(self, chat_id, *, title="", chat_type=""):
        resolved = await super().bind_group(chat_id, title=title, chat_type=chat_type)
        record = self.state.get("first_message", {}) or {}
        if record.get("chat_id") and int(record.get("chat_id")) != int(resolved):
            # Keep the operator's text, but create a fresh pinned message in the new group.
            self.state.set("first_message", {})
        return resolved

    async def _send_first(self, text):
        target = await self.ensure_target()
        return await self.api(
            "sendMessage",
            chat_id=target,
            text=text,
            disable_web_page_preview=True,
        )

    async def _edit_first(self, message_id, text):
        target = await self.ensure_target()
        return await self.api(
            "editMessageText",
            chat_id=target,
            message_id=int(message_id),
            text=text,
            disable_web_page_preview=True,
        )

    async def _pin_first(self, message_id):
        target = await self.ensure_target()
        return await self.api(
            "pinChatMessage",
            chat_id=target,
            message_id=int(message_id),
            disable_notification=True,
        )

    async def _before_recreate_first(self):
        """Make room for a recreated intro so it is physically before price posts.

        CatalogPublisher extends this hook to remove its navigation message too.
        """
        return await self._clear_managed_price_posts()

    async def _ensure_first_message(self, *, strict_pin=False):
        text = str(self.state.get("first_message_text", "") or "").strip()
        if not text:
            return 0

        async with self.first_message_lock:
            target = await self.ensure_target()
            binding = self.binding()
            record = self.state.get("first_message", {}) or {}
            if record.get("binding") != binding or int(record.get("chat_id") or 0) != int(target):
                record = {}

            message_id = record.get("id")
            content_hash = digest(text)
            changes = 0

            # Always verify the stored Telegram message, even when the text/hash did
            # not change. Older versions trusted a saved ID forever, so a deleted
            # intro could remain "present" in state while no message existed at all.
            if message_id:
                try:
                    await self._edit_first(message_id, text)
                    if record.get("hash") != content_hash:
                        changes += 1
                except RuntimeError as exc:
                    lowered = str(exc).lower()
                    if "message is not modified" in lowered:
                        # This is the normal existence check for unchanged text.
                        pass
                    elif is_missing_message_error(exc):
                        message_id = None
                    else:
                        raise

            if not message_id:
                # A missing/stale intro must be recreated before all managed content,
                # not merely appended after the current price posts.
                await self._before_recreate_first()
                message = await self._send_first(text)
                message_id = int(message["message_id"])
                changes += 1
                record = {}

            pinned = bool(record.get("pinned"))
            pin_error = ""
            if not pinned:
                try:
                    await self._pin_first(message_id)
                    pinned = True
                except RuntimeError as exc:
                    pin_error = str(exc)

            self.state.set("first_message", {
                "binding": binding,
                "chat_id": int(target),
                "id": int(message_id),
                "hash": content_hash,
                "text": text,
                "pinned": pinned,
                "pin_error": pin_error,
            })
            if strict_pin and pin_error:
                raise RuntimeError("Первое сообщение отправлено, но не закрепилось. "
                                   "Дай управляющему боту право закреплять сообщения. " + pin_error)
            return changes

    async def _clear_managed_price_posts(self):
        """Remove current managed price posts so a new intro becomes physically first."""
        async with self.lock:
            await self.ensure_target()
            binding = self.binding()
            stored = self.state.get("published", {}) or {}
            manifest = stored.get("messages", {}) if stored.get("binding") == binding else {}
            deleted = 0
            for key, entry in list(manifest.items()):
                try:
                    await self._delete(entry["id"])
                    deleted += 1
                    await asyncio.sleep(max(0, self.settings.send_delay))
                except RuntimeError as exc:
                    if not is_missing_message_error(exc):
                        raise
                del manifest[key]
                self.state.set("published", {"binding": binding, "messages": manifest})
            return deleted

    async def set_first_message(self, text):
        """Save, publish and pin the operator's first message.

        On first creation, existing managed price posts are recreated after this message,
        making it the first message of the bot-managed price sequence as well as pinned.
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("Первое сообщение не может быть пустым")
        if len(text) > 3900:
            raise ValueError("Первое сообщение слишком длинное; максимум около 3900 символов")

        await self.ensure_target()
        previous = self.state.get("first_message", {}) or {}
        is_new_here = (
            not previous.get("id")
            or previous.get("binding") != self.binding()
            or int(previous.get("chat_id") or 0) != int(self.target)
        )
        self.state.set("first_message_text", text)
        if is_new_here:
            await self._clear_managed_price_posts()
        changes = await self._ensure_first_message(strict_pin=True)
        return changes

    async def publish(self, pages):
        # Always establish the custom pinned message before sending/recreating price blocks.
        with suppress(RuntimeError):
            await self._ensure_first_message(strict_pin=False)
        return await super().publish(pages)
