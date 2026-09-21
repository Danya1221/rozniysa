"""Editable retail posts, two brand menus and replaceable photographic covers."""
import asyncio
import html
import json
import logging
import time
from contextlib import suppress

import aiohttp

from bot_publisher import BotAPIPublisher, BotAPIRejected, digest, plain
from catalog_publisher import message_link
from retail_catalog import cover_bytes

log = logging.getLogger(__name__)


def missing(exc):
    return any(s in str(exc).lower() for s in ("not found", "message_id_invalid", "message id invalid"))


class RetailPublisher(BotAPIPublisher):
    def __init__(self, token, target, state, settings, retail):
        super().__init__(token, target, state, settings)
        self.retail = retail
        self.client = None
        self.navigation = {}
        self.layout_lock = asyncio.Lock()

    async def bind_group(self, chat_id, *, title="", chat_type=""):
        if self.layout_lock.locked():
            raise RuntimeError("Идёт публикация прайса. Дождись завершения и привяжи группу ещё раз")
        async with self.layout_lock:
            return await super().bind_group(chat_id, title=title, chat_type=chat_type)

    async def cleanup_retired_test_posts(self):
        """One-time cleanup of untracked posts from the retired test button."""
        if not self.client or self.state.get("retired_test_cleaned") == self.binding():
            return
        try:
            messages = await self.client.get_messages(self.target, limit=100, search="ТЕСТОВАЯ ПОЗИЦИЯ")
            for message in messages:
                if getattr(message, "sender_id", None) != self.bot_id:
                    continue
                text = (getattr(message, "raw_text", "") or "").strip()
                if text != "ТЕСТОВАЯ ПОЗИЦИЯ\n\niPhone 17 256GB Blue Sim+eSim — 150 000":
                    continue
                urls = [getattr(entity, "url", "") for entity in getattr(message, "entities", ()) or ()]
                if not any(url.endswith("?start=p_15dcc99ee2ffb0dca9788f6a") for url in urls):
                    continue
                try:
                    await self._delete(message.id)
                except RuntimeError as exc:
                    if not missing(exc):
                        raise
                await asyncio.sleep(self.settings.send_delay)
            # If a large number of old tests existed, clean the next batch later.
            if len(messages) < 100:
                self.state.set("retired_test_cleaned", self.binding())
        except Exception as exc:
            # Price updates do not depend on optional access to old test history.
            log.warning("Очистка старых тестовых постов отложена: %s", type(exc).__name__)

    def arrange_manifest(self, pages, manifest):
        # Telegram has no move operation. Reuse chronological text slots.
        slots = sorted(manifest.values(), key=lambda entry: int(entry["id"]))
        arranged = {key: entry for key, entry in zip(pages, slots)}
        arranged.update({"obsolete:" + str(entry["id"]): entry for entry in slots[len(pages):]})
        return arranged

    def nodes(self):
        stored = self.state.get("retail_nodes", {})
        return stored.get("nodes", {}) if stored.get("binding") == self.binding() else {}

    def save_nodes(self, nodes):
        self.state.set("retail_nodes", {"binding": self.binding(), "nodes": nodes})

    async def _edit(self, message_id, content):
        try:
            return await super()._edit(message_id, content)
        except RuntimeError as exc:
            if any(t in str(exc).lower() for t in ("can't be edited", "cannot be edited")):
                raise RuntimeError("Telegram запретил изменение сообщения: проверь права бота") from None
            raise

    async def _delete(self, message_id):
        try:
            return await super()._delete(message_id)
        except RuntimeError as exc:
            if not any(t in str(exc).lower() for t in ("can't be deleted", "cannot be deleted")):
                raise
            root = self.nodes().get("root:0", {}).get("id")
            rows = [[{"text": "Актуальный каталог", "url": message_link(self.target, root)}]] if root else []
            caption = any(n.get("id") == message_id and n.get("photo") for n in self.nodes().values())
            try:
                return await self.api("editMessageCaption" if caption else "editMessageText", chat_id=self.target,
                                      message_id=message_id, reply_markup={"inline_keyboard": rows},
                                      **({"caption": "Раздел обновлён."} if caption else {"text": "Раздел обновлён."}))
            except RuntimeError as edit_exc:
                if "not modified" not in str(edit_exc).lower() and not missing(edit_exc):
                    raise

    async def hide_existing(self):
        await asyncio.to_thread(self.retail.mark_uncertain, "Поставщик закрыт: наличие подтверждает менеджер")
        return 0

    async def _verify_or_rebuild_manifest(self, pages, manifest, binding):
        # Periodic probes recover deleted posts, without rebuilding all live prices.
        if time.time() - self.state.get("retail_probe", 0) < 3600:
            return 0
        for key, entry in list(manifest.items()):
            if key not in pages:
                continue
            try:
                await self._edit(entry["id"], entry.get("content", pages[key]))
            except RuntimeError as exc:
                if "not modified" in str(exc).lower():
                    pass
                elif missing(exc):
                    del manifest[key]
                else:
                    raise
            await asyncio.sleep(self.settings.send_delay)
        self.state.set("retail_probe", time.time())
        self.state.set("published", {"binding": binding, "messages": manifest})
        return 0

    async def _read_pending_history(self):
        try:
            return await self.client.get_messages(self.target, limit=300)
        except ValueError:
            # StringSession keeps authorization, not the full entity cache. On a
            # redeploy a numeric group ID may need its access hash resolved again.
            await asyncio.wait_for(self.client.get_dialogs(limit=None), timeout=30)
            try:
                return await self.client.get_messages(self.target, limit=300)
            except ValueError:
                chat = await self.api("getChat", chat_id=self.target)
                if chat.get("username"):
                    entity = await self.client.get_entity("@" + chat["username"])
                    from telethon.utils import get_peer_id
                    if get_peer_id(entity) == self.target:
                        return await self.client.get_messages(entity, limit=300)
                raise

    async def recover_pending(self):
        for field in ("pending_publish", "pending_retail_node"):
            pending = self.state.get(field)
            if not pending or pending.get("binding") != self.binding():
                continue
            if not self.client:
                raise RuntimeError("Подключи Telegram-аккаунт 1 через /login 1 для проверки предыдущей отправки. "
                                   "Либо открой «Восстановить публикацию» и проверь сообщение в группе")
            try:
                messages = await self._read_pending_history()
            except Exception as exc:
                log.warning("Проверка истории группы не завершена: %s", type(exc).__name__)
                if type(exc).__name__ in {"ValueError", "ChannelPrivateError", "ChatAdminRequiredError", "PeerIdInvalidError"}:
                    raise RuntimeError("Группа привязана к боту. Для проверки истории добавь в неё Telegram-аккаунт 1, "
                                       "которым вошёл через /login 1, и снова нажми «Запросить сейчас». "
                                       "Либо открой «Восстановить публикацию» и проверь сообщение вручную") from None
                raise RuntimeError("Telegram временно не дал проверить историю группы. Попробуй ещё раз; "
                                   "для ручной проверки открой «Восстановить публикацию»") from None
            known_ids = {v["id"] for v in self.nodes().values() if v.get("id")}
            stored = self.state.get("published", {}) or {}
            if stored.get("binding") == self.binding():
                known_ids.update(v["id"] for v in stored.get("messages", {}).values() if v.get("id"))
            matches = []
            for message in messages:
                sender = getattr(message, "sender_id", None)
                our_post = sender == self.bot_id or (self.target_kind == "channel"
                    and sender == self.target and getattr(message, "post", False))
                if not our_post or message.id in known_ids or message.id <= pending.get("after_id", 0):
                    continue
                sent_at = getattr(message, "date", None)
                if sent_at and pending.get("started_at") and sent_at.timestamp() < pending["started_at"] - 5:
                    continue
                if (getattr(message, "raw_text", "") or "") == plain(pending["text"]):
                    matches.append(message)
            if len(matches) != 1:
                raise RuntimeError("Не удалось однозначно определить предыдущую отправку. "
                                   "Открой «Восстановить публикацию» или /retry_publish и проверь сообщение в группе")
            message = matches[0]
            if field == "pending_publish":
                stored = self.state.get("published", {})
                manifest = stored.get("messages", {}) if stored.get("binding") == self.binding() else {}
                manifest[pending["key"]] = {"id": message.id, "content": pending["text"], "hash": digest(pending["text"])}
                self.state.set("published", {"binding": self.binding(), "messages": manifest})
            else:
                nodes = self.nodes()
                nodes[pending["key"]] = {"id": message.id, "hash": "", "photo": pending.get("photo", False)}
                self.save_nodes(nodes)
            self.state.set(field, None)

    async def _photo(self, caption, brand, keyboard, photo_id=None):
        if photo_id:
            return await self.api("sendPhoto", chat_id=self.target, photo=photo_id,
                                  caption=caption, parse_mode="HTML", reply_markup=keyboard)

        photo = await asyncio.to_thread(cover_bytes, brand)
        max_attempts = 5
        for attempt in range(max_attempts):
            if self.http is None or self.http.closed:
                self.http = self._new_http()

            form = aiohttp.FormData()
            form.add_field("chat_id", str(self.target))
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
            form.add_field("reply_markup", json.dumps(keyboard, ensure_ascii=False))
            form.add_field("photo", photo, filename="cover.jpg", content_type="image/jpeg")

            try:
                async with self.http.post(self.base + "/sendPhoto", data=form) as response:
                    result = await response.json(content_type=None)
                    status = response.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # sendPhoto has an ambiguous result after a transport failure:
                # Telegram may already have accepted the image, so never blindly
                # repeat it and risk a duplicate cover post.
                raise RuntimeError(
                    "Telegram не подтвердил отправку обложки; повтор только после проверки истории"
                ) from exc

            if result.get("ok"):
                return result["result"]

            parameters = result.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            if (status == 429 or retry_after is not None) and attempt < max_attempts - 1:
                try:
                    wait_seconds = max(1, int(retry_after or 1))
                except (TypeError, ValueError):
                    wait_seconds = 1
                await asyncio.sleep(wait_seconds + 1)
                continue

            # A 5xx may arrive after Telegram accepted the photo. Leave the
            # pending node for history recovery instead of sending another copy.
            if status >= 500:
                raise RuntimeError("Telegram не подтвердил отправку обложки; повтор только после проверки истории")

            code = result.get("error_code", status)
            rejected = result.get("ok") is False and isinstance(code, int) and 400 <= code < 500
            error = BotAPIRejected if rejected else RuntimeError
            raise error(str(result.get("description", "Не удалось отправить обложку")))

        raise RuntimeError("Telegram не принял обложку после нескольких попыток")

    async def node(self, key, text, rows, brand=None):
        nodes = self.nodes()
        record = nodes.get(key, {})
        covers = self.state.get("retail_covers", {})
        photo_id = covers.get(brand, "") if brand else ""
        fingerprint = digest(json.dumps([text, rows, photo_id], ensure_ascii=False))
        keyboard = {"inline_keyboard": rows}
        if record.get("id") and record.get("hash") == fingerprint and time.time() - record.get("checked", 0) < 3600:
            return record["id"]
        if record.get("id"):
            try:
                if brand and photo_id and record.get("photo_id") != photo_id:
                    await self.api("editMessageMedia", chat_id=self.target, message_id=record["id"],
                                   media={"type": "photo", "media": photo_id, "caption": text, "parse_mode": "HTML"},
                                   reply_markup=keyboard)
                else:
                    await self.api("editMessageCaption" if brand else "editMessageText", chat_id=self.target,
                                   message_id=record["id"], parse_mode="HTML", reply_markup=keyboard,
                                   **({"caption": text} if brand else {"text": text}))
            except RuntimeError as exc:
                if "not modified" in str(exc).lower():
                    pass
                elif missing(exc):
                    record = {}
                else:
                    raise
        if not record.get("id"):
            self.state.set("pending_retail_node", self.pending_send(key, text, photo=bool(brand)))
            try:
                message = (await self._photo(text, brand, keyboard, photo_id or None) if brand else
                           await self.api("sendMessage", chat_id=self.target, text=text, parse_mode="HTML", reply_markup=keyboard))
            except BotAPIRejected:
                self.state.set("pending_retail_node", None)
                raise
            record = {"id": int(message["message_id"])}
        record.update(hash=fingerprint, photo=bool(brand), photo_id=photo_id, checked=time.time(),
                      text=text, rows=rows)
        nodes[key] = record
        self.state.update({"retail_nodes": {"binding": self.binding(), "nodes": nodes}, "pending_retail_node": None})
        await asyncio.sleep(self.settings.send_delay)
        return record["id"]

    async def set_first_message(self, text):
        if not text.strip() or len(text) > 2500:
            raise ValueError("Текст приветствия: от 1 до 2500 символов")
        self.state.set("first_message_text", text.strip())
        self.state.set("retail_probe", 0)
        return 0

    async def publish(self, pages):
        async with self.layout_lock:
            await self.ensure_target()
            if not message_link(self.target, 1):
                raise RuntimeError("Для переходов по разделам выбери канал или супергруппу Telegram")
            await self.recover_pending()
            root_ids = []
            for index in range(2):
                key = "root:" + str(index)
                existing = self.nodes().get(key, {})
                root_ids.append(await self.node(key, existing.get("text", f"Каталог · часть {index+1}\nРазделы обновляются…"), existing.get("rows", [])))
            brand_ids = {}
            for brand in self.navigation:
                key = "brand:" + digest(brand)[:16]
                existing = self.nodes().get(key, {})
                brand_ids[brand] = await self.node(key, existing.get("text", "<b>" + html.escape(brand) + "</b>\nВыбери раздел ниже."), existing.get("rows", []), brand)
            changes = await super().publish(pages)
            manifest = self.state.get("published", {}).get("messages", {})
            keyboard_hashes = self.state.get("retail_price_keyboards", {})
            for brand, sections in self.navigation.items():
                buttons = []
                for section in sections:
                    keys = section["keys"]
                    if keys and keys[0] in manifest:
                        buttons.append({"text": section["section"], "url": message_link(self.target, manifest[keys[0]]["id"])})
                    for index, key in enumerate(keys):
                        if key not in manifest:
                            continue
                        rows, arrows = [], []
                        for other, label in ((index-1, "← Предыдущая часть"), (index+1, "Следующая часть →")):
                            if 0 <= other < len(keys) and keys[other] in manifest:
                                arrows.append({"text": label, "url": message_link(self.target, manifest[keys[other]]["id"])})
                        if arrows:
                            rows.append(arrows)
                        rows.append([{"text": brand, "url": message_link(self.target, brand_ids[brand])},
                                     {"text": "Все бренды", "url": message_link(self.target, root_ids[0])}])
                        fingerprint = digest(json.dumps([manifest[key]["id"], manifest[key]["hash"], rows]))
                        if keyboard_hashes.get(key) != fingerprint:
                            try:
                                await self.api("editMessageReplyMarkup", chat_id=self.target, message_id=manifest[key]["id"],
                                               reply_markup={"inline_keyboard": rows})
                            except RuntimeError as exc:
                                if "not modified" not in str(exc).lower():
                                    raise
                            keyboard_hashes[key] = fingerprint
                            await asyncio.sleep(self.settings.send_delay)
                rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
                rows.append([{"text": "← Все бренды", "url": message_link(self.target, root_ids[0])}])
                await self.node("brand:" + digest(brand)[:16], "<b>" + html.escape(brand) + "</b>\nВыбери раздел прайса.", rows, brand)
            self.state.set("retail_price_keyboards", {k: v for k, v in keyboard_hashes.items() if k in manifest})
            brands = list(self.navigation)
            middle = max(1, (len(brands)+1)//2)
            for index, group in enumerate((brands[:middle], brands[middle:])):
                buttons = [{"text": name, "url": message_link(self.target, brand_ids[name])} for name in group]
                rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
                rows.append([{"text": "Другие бренды →" if index == 0 else "← Первая часть", "url": message_link(self.target, root_ids[1-index])}])
                text = (html.escape(self.state.get("first_message_text", "Каталог техники")) if index == 0 else "<b>Каталог · другие бренды</b>")
                text += "\n\nВыбери бренд, затем раздел. Нажми на товар в прайсе, чтобы оформить заявку."
                if not group:
                    text += "\nНовые разделы появятся вместе с товарами."
                await self.node("root:" + str(index), text, rows)
            # Remove obsolete cover posts when a brand is disabled or disappears.
            live = {"root:0", "root:1"} | {"brand:" + digest(name)[:16] for name in brands}
            nodes = self.nodes()
            for key in list(nodes):
                if key not in live:
                    try:
                        await self._delete(nodes[key]["id"])
                    except RuntimeError as exc:
                        if not missing(exc):
                            raise
                    del nodes[key]
            self.save_nodes(nodes)
            pinned = {"binding": self.binding(), "id": root_ids[0]}
            if self.state.get("retail_pinned") != pinned:
                await self.api("pinChatMessage", chat_id=self.target, message_id=root_ids[0], disable_notification=True)
                self.state.set("retail_pinned", pinned)
            await asyncio.to_thread(self.retail.set, "system", "catalog_url", message_link(self.target, root_ids[0]))
            await self.cleanup_retired_test_posts()
            return changes
