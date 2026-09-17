"""Publish managed price messages through Telegram Bot API.

Supplier access is handled separately through the logged-in user account. The place
where the finished price is published is a Telegram group/supergroup/channel, never a
private control chat. A group can be bound from Telegram with /bind and the binding is
stored in the persistent state, so Railway redeploys do not lose it.
"""
import asyncio
import hashlib
import html
import re
from contextlib import suppress

import aiohttp
from telethon.extensions import html as telegram_html


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain(text):
    return telegram_html.parse(text)[0]


def is_missing_message_error(error):
    """Telegram uses several texts for a stale/deleted stored message ID."""
    text = str(error).lower().replace("-", "_")
    return any(marker in text for marker in (
        "message to edit not found",
        "message to delete not found",
        "message_id_invalid",
        "message id invalid",
        "message identifier is not specified",
        "message can't be edited",
        "message cannot be edited",
    ))


class BotAPIPublisher:
    def __init__(self, token, target, state, settings):
        self.token = (token or "").strip()
        self.base = f"https://api.telegram.org/bot{self.token}"
        self.configured_target = target
        self.target = None
        self.state = state
        self.settings = settings
        self.http = None
        self.lock = asyncio.Lock()
        self.bot_id = None
        self.bot_username = ""
        self.target_kind = ""
        self.target_title = ""
        # Kept only for backward-compatible diagnostics/tests. Private fallback is
        # intentionally disabled; finished prices must go to a bound group/channel.
        self.used_admin_fallback = False

    def _new_http(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))

    async def _reset_http(self):
        old = self.http
        self.http = None
        if old is not None and not old.closed:
            with suppress(Exception):
                await old.close()

    @staticmethod
    def _transport_retry_safe(method, exc):
        # Repeating sendMessage after a dropped response can create a duplicate:
        # Telegram may have accepted the message even though aiohttp never saw the
        # response. Connector failures happen before a request is sent and are safe.
        if isinstance(exc, aiohttp.ClientConnectorError):
            return True
        return method not in {"sendMessage", "sendPhoto", "copyMessage", "forwardMessage"}

    async def api(self, method, **payload):
        """Call Bot API with 429, HTTP 5xx and transient transport retries.

        Telegram occasionally closes an idle keep-alive connection and aiohttp raises
        ServerDisconnectedError. For idempotent Bot API calls we discard that stale
        ClientSession, reconnect and retry instead of failing the whole price sync.
        """
        if not self.token:
            raise RuntimeError("BOT_TOKEN не задан: без него нельзя публиковать прайс")

        max_attempts = 5
        for attempt in range(max_attempts):
            if self.http is None or self.http.closed:
                self.http = self._new_http()
            try:
                async with self.http.post(f"{self.base}/{method}", json=payload) as response:
                    data = await response.json(content_type=None)
                    status = response.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                retry_safe = self._transport_retry_safe(method, exc)
                await self._reset_http()
                if retry_safe and attempt < max_attempts - 1:
                    await asyncio.sleep(min(1 + attempt, 3))
                    continue
                raise RuntimeError(
                    f"Bot API {method}: временный сбой соединения: {type(exc).__name__}: {exc}"
                ) from exc

            if data.get("ok"):
                return data.get("result")

            parameters = data.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            throttled = status == 429 or retry_after is not None
            if throttled and attempt < max_attempts - 1:
                try:
                    wait_seconds = max(1, int(retry_after or 1))
                except (TypeError, ValueError):
                    wait_seconds = 1
                await asyncio.sleep(wait_seconds + 1)
                continue

            if status in {500, 502, 503, 504} and method not in {"sendMessage", "sendPhoto", "copyMessage", "forwardMessage"} and attempt < max_attempts - 1:
                await self._reset_http()
                await asyncio.sleep(min(1 + attempt, 3))
                continue

            raise RuntimeError(f"Bot API {method}: {data.get('description', data)}")

        raise RuntimeError(f"Bot API {method}: превышено число повторных попыток")

    def _target_candidates(self):
        value = self.configured_target
        if isinstance(value, str):
            text = value.strip()
            match = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/?", text, re.I)
            if match:
                return ["@" + match.group(1)]
            if text.startswith("@"):
                return [text]
            if re.fullmatch(r"-?\d+", text):
                value = int(text)
            elif text:
                return [text]
            else:
                return []
        if isinstance(value, int):
            if value > 0:
                # A positive raw channel/supergroup ID is sometimes pasted without
                # the Bot API -100 prefix. Try the channel form first.
                return [int(f"-100{value}"), value]
            return [value]
        return [value] if value is not None else []

    async def _bot_identity(self):
        if self.bot_id is None:
            me = await self.api("getMe")
            self.bot_id = int(me["id"])
            self.bot_username = me.get("username") or ""

    async def _usable_chat(self, candidate):
        """Return a writable group/channel or (None, reason).

        Private users and supplier bots are deliberately rejected. This guarantees
        that a wrong TARGET_CHANNEL can never make the ready price appear in the
        administrator's private bot chat again.
        """
        try:
            chat = await self.api("getChat", chat_id=candidate)
        except Exception as exc:
            return None, f"{candidate!r}: {exc}"

        chat_type = chat.get("type")
        chat_id = int(chat["id"])
        if chat_type not in {"channel", "supergroup", "group"}:
            return None, f"{candidate!r}: это не группа/канал, а {chat_type or 'неизвестный чат'}"

        await self._bot_identity()
        try:
            member = await self.api("getChatMember", chat_id=chat_id, user_id=self.bot_id)
        except Exception as exc:
            return None, f"{candidate!r}: не удалось проверить права бота: {exc}"

        status = member.get("status")
        if chat_type == "channel":
            can_write = status == "creator" or (
                status == "administrator" and member.get("can_post_messages", True) is not False
            )
        else:
            can_write = status in {"creator", "administrator", "member"}
            if status == "restricted":
                can_write = member.get("is_member", False) and member.get("can_send_messages", False)

        if not can_write:
            return None, f"{candidate!r}: управляющий бот не может писать в эту группу/канал"
        return chat, None

    async def bind_group(self, chat_id, *, title="", chat_type=""):
        """Persistently bind publishing to the group where /bind was sent."""
        chat, reason = await self._usable_chat(int(chat_id))
        if chat is None:
            raise RuntimeError(reason or "Не удалось привязать группу")

        resolved_id = int(chat["id"])
        resolved_type = chat.get("type") or chat_type or ""
        resolved_title = chat.get("title") or title or str(resolved_id)
        self.state.set("publish_target", {
            "chat_id": resolved_id,
            "type": resolved_type,
            "title": resolved_title,
        })
        self.state.set("botapi_target", {})
        self.target = None
        self.target_kind = ""
        self.target_title = ""
        await self.ensure_target()
        return resolved_id

    async def ensure_target(self):
        if self.target is not None:
            return self.target

        await self._bot_identity()
        candidates = []
        bound = self.state.get("publish_target", {}) or {}
        if bound.get("chat_id"):
            candidates.append(int(bound["chat_id"]))

        cached = self.state.get("botapi_target", {}) or {}
        if cached.get("configured") == str(self.configured_target) and cached.get("chat_id"):
            cached_id = int(cached["chat_id"])
            if cached_id not in candidates:
                candidates.append(cached_id)

        for candidate in self._target_candidates():
            if candidate not in candidates:
                candidates.append(candidate)

        reasons = []
        chosen = None
        for candidate in candidates:
            chat, reason = await self._usable_chat(candidate)
            if chat is not None:
                chosen = chat
                break
            if reason:
                reasons.append(reason)

        if chosen is None:
            bot = f"@{self.bot_username}" if self.bot_username else "управляющего бота"
            details = " | ".join(reasons[-3:])
            suffix = f" Последняя проверка: {details}" if details else ""
            raise RuntimeError(
                "Группа для публикации не привязана. Добавь " + bot +
                " в нужную группу и отправь в этой группе /bind от аккаунта из ADMIN_IDS." + suffix
            )

        chat_id = int(chosen["id"])
        chat_type = chosen.get("type") or ""
        title = chosen.get("title") or chosen.get("username") or str(chat_id)
        self.target = chat_id
        self.target_kind = chat_type
        self.target_title = title
        self.state.set("botapi_target", {
            "configured": str(self.configured_target),
            "chat_id": chat_id,
            "type": chat_type,
            "title": title,
        })
        # Once a real group/channel was found, remember it as the authoritative
        # destination even if TARGET_CHANNEL in Railway is stale or points elsewhere.
        self.state.set("publish_target", {
            "chat_id": chat_id,
            "type": chat_type,
            "title": title,
        })
        return chat_id

    def binding(self):
        value = self.target if self.target is not None else self.configured_target
        return f"botapi:{value}"

    def destination_note(self):
        if self.target is None:
            bound = self.state.get("publish_target", {}) or {}
            if bound.get("chat_id"):
                return f"группа публикации: {bound.get('title') or bound['chat_id']}"
            return "группа публикации ещё не привязана"
        return f"прайс публикуется в {self.target_title or self.target}"

    async def _send(self, content):
        target = await self.ensure_target()
        return await self.api(
            "sendMessage",
            chat_id=target,
            text=content,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _edit(self, message_id, content):
        target = await self.ensure_target()
        return await self.api(
            "editMessageText",
            chat_id=target,
            message_id=int(message_id),
            text=content,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _delete(self, message_id):
        target = await self.ensure_target()
        return await self.api("deleteMessage", chat_id=target, message_id=int(message_id))

    async def hide_existing(self):
        await self.ensure_target()
        stored = self.state.get("published", {})
        manifest = stored.get("messages", {}) if stored.get("binding") == self.binding() else {}
        if not manifest:
            return 0
        pages = {}
        for key, entry in manifest.items():
            old = entry.get("content") or html.escape(self.settings.header)
            heading = old.split("\n\n", 1)[0]
            pages[key] = heading + "\n\nПродажи закрыты"
        return await self.publish(pages)

    def arrange_manifest(self, pages, manifest):
        return manifest

    async def _verify_or_rebuild_manifest(self, pages, manifest, binding):
        """Keep stored IDs while they exist; rebuild all managed price posts if one was deleted.

        Bot API has no getMessage method. Editing a message with its current stored
        content is therefore the safest existence probe: a live unchanged message
        returns `message is not modified`, while a deleted/stale ID returns one of
        Telegram's invalid-message errors. Rebuilding the whole managed price set
        preserves the requested physical order instead of appending one recovered
        block at the bottom.
        """
        if not manifest:
            return 0

        missing = False
        for key, entry in list(manifest.items()):
            if key not in pages or not entry.get("id"):
                continue
            probe_content = entry.get("content")
            if not probe_content:
                continue
            try:
                await self._edit(entry["id"], probe_content)
            except RuntimeError as exc:
                text = str(exc).lower()
                if "message is not modified" in text:
                    continue
                if is_missing_message_error(exc):
                    missing = True
                    break
                raise

        if not missing:
            return 0

        # Availability-safe rebuild: never erase the last complete price before a
        # replacement exists. Keep the old manifest as a staging snapshot, publish
        # a complete fresh set under new IDs, and delete the old live posts only
        # after every replacement page has been checkpointed successfully.
        snapshot = {key: dict(entry) for key, entry in manifest.items()}
        self.state.set("rebuild_old_manifest", {
            "binding": binding,
            "messages": snapshot,
        })
        manifest.clear()
        self.state.set("published", {"binding": binding, "messages": manifest})
        return 0

    async def publish(self, pages):
        async with self.lock:
            await self.ensure_target()
            binding = self.binding()
            stored = self.state.get("published", {})
            manifest = stored.get("messages", {}) if stored.get("binding") == binding else {}
            rebuild_state = self.state.get("rebuild_old_manifest", {}) or {}
            rebuilding = (
                rebuild_state.get("binding") == binding
                and bool(rebuild_state.get("messages"))
            )
            # During a staged rebuild, the current manifest is the partial/new set.
            # Do not remap those checkpointed new IDs onto different page keys.
            if not rebuilding:
                manifest = self.arrange_manifest(pages, manifest)
            self.state.set("published", {"binding": binding, "messages": manifest})
            changes = 0
            if not rebuilding:
                changes = await self._verify_or_rebuild_manifest(pages, manifest, binding)

            for key, content in pages.items():
                entry = manifest.get(key)
                message_id = entry.get("id") if entry else None
                content_hash = digest(content)
                unchanged = bool(
                    message_id and entry and (
                        entry.get("hash") == content_hash or entry.get("content") == content
                    )
                )
                changed_this_page = False

                # Most syncs change only a few price blocks. The old implementation
                # edited every message on every run, which quickly hit Telegram's
                # editMessageText flood limit. Identical pages now make zero API calls.
                if message_id and not unchanged:
                    try:
                        await self._edit(message_id, content)
                        changes += 1
                        changed_this_page = True
                    except RuntimeError as exc:
                        text = str(exc).lower()
                        if "message is not modified" in text:
                            pass
                        elif is_missing_message_error(exc):
                            message_id = None
                        else:
                            raise

                if not message_id:
                    self.state.set("pending_publish", {
                        "binding": binding,
                        "key": key,
                        "text": content,
                    })
                    message = await self._send(content)
                    message_id = int(message["message_id"])
                    changes += 1
                    changed_this_page = True

                manifest[key] = {
                    "id": int(message_id),
                    "hash": content_hash,
                    "content": content,
                }
                self.state.update({
                    "published": {"binding": binding, "messages": manifest},
                    "pending_publish": None,
                })
                if changed_this_page:
                    await asyncio.sleep(max(0, self.settings.send_delay))

            for key in list(manifest):
                if key in pages:
                    continue
                deleted = False
                try:
                    await self._delete(manifest[key]["id"])
                    deleted = True
                except RuntimeError as exc:
                    if not is_missing_message_error(exc):
                        raise
                del manifest[key]
                self.state.set("published", {"binding": binding, "messages": manifest})
                changes += 1
                if deleted:
                    await asyncio.sleep(max(0, self.settings.send_delay))

            # Commit a staged rebuild only after the complete new page set exists.
            # If any send/edit above raises, execution never reaches this block and
            # the previous live price remains untouched for the next retry.
            rebuild_state = self.state.get("rebuild_old_manifest", {}) or {}
            if rebuild_state.get("binding") == binding and rebuild_state.get("messages"):
                new_ids = {int(entry["id"]) for entry in manifest.values() if entry.get("id")}
                for entry in rebuild_state.get("messages", {}).values():
                    old_id = entry.get("id")
                    if not old_id or int(old_id) in new_ids:
                        continue
                    deleted = False
                    try:
                        await self._delete(old_id)
                        deleted = True
                    except RuntimeError as exc:
                        if not is_missing_message_error(exc):
                            raise
                    changes += 1
                    if deleted:
                        await asyncio.sleep(max(0, self.settings.send_delay))
                self.state.set("rebuild_old_manifest", {})

            return changes

    async def close(self):
        if self.http is not None and not self.http.closed:
            await self.http.close()
