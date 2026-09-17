"""Edit only managed messages; checkpoint every successful Telegram mutation."""
import asyncio
import hashlib
import html
import re

from telethon.errors import MessageNotModifiedError
from telethon.extensions import html as telegram_html


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain(text):
    return telegram_html.parse(text)[0]


class Publisher:
    def __init__(self, client, target, state, settings):
        self.client = client
        self.target = target
        self.state = state
        self.settings = settings
        self.lock = asyncio.Lock()

    def binding(self):
        return str(self.target)

    async def _recover(self, pages, manifest):
        """Recover only exact recognizable price pages authored by this account."""
        expected = {}
        for key, content in pages.items():
            # Title/part are stable even when prices change.
            prefix = plain(content).split("\n\n", 2)
            expected["\n\n".join(prefix[:2])] = key
        pending = self.state.get("pending_publish")
        recovered = set()
        me = await self.client.get_me()
        # Search every page with our header, not the last 100 arbitrary channel posts.
        async for message in self.client.iter_messages(self.target, search=self.settings.header):
            if not (message.out or message.sender_id == me.id):
                continue
            text = message.raw_text or ""
            if not text.startswith(self.settings.header + "\n\n"):
                continue
            prefix = "\n\n".join(text.split("\n\n", 2)[:2])
            key = expected.get(prefix)
            if pending and pending.get("binding") == self.binding() and text == plain(pending["text"]):
                key = pending["key"]
            if key and key not in recovered and (key not in manifest or (pending and pending.get("key") == key)):
                manifest[key] = {"id": message.id, "hash": None}
                recovered.add(key)
        # Migrate the original one-message bot in place when its saved post is present.
        legacy_id = self.state.get("target_message_id")
        if not manifest and pages and legacy_id:
            legacy = await self.client.get_messages(self.target, ids=int(legacy_id))
            if (legacy and (legacy.out or legacy.sender_id == me.id)
                    and (legacy.raw_text or "").startswith(self.settings.header + "\n")):
                manifest[next(iter(pages))] = {"id": legacy.id, "hash": None}
        self.state.set("published", {"binding": self.binding(), "messages": manifest})

    async def hide_existing(self):
        """First deployment at night: hide managed prices even without an item cache."""
        stored = self.state.get("published", {})
        manifest = stored.get("messages", {}) if stored.get("binding") == self.binding() else {}
        keys = {entry["id"]: key for key, entry in manifest.items()}
        messages = []
        if keys:
            messages = await self.client.get_messages(self.target, ids=list(keys))
        else:
            me = await self.client.get_me()
            async for message in self.client.iter_messages(self.target, search=self.settings.header):
                if ((message.out or message.sender_id == me.id)
                        and (message.raw_text or "").startswith(self.settings.header + "\n\n")):
                    messages.append(message)
        pages = {}
        recovered = {}
        for message in messages:
            if not message:
                continue
            sections = (message.raw_text or "").split("\n\n", 2)
            # Only a genuine managed block heading can be retained; never retain product lines.
            title = sections[1] if len(sections) > 1 else ""
            match = re.fullmatch(r"— (.+) —(?:\nЧасть (\d+))?", title)
            if match:
                key = hashlib.sha256(match[1].encode()).hexdigest()[:16] + ":" + str(int(match[2] or 1)-1)
                heading = html.escape(self.settings.header) + "\n\n<b>— " + html.escape(match[1]) + " —</b>"
                if match[2]:
                    heading += "\nЧасть " + match[2]
            else:
                key = keys.get(message.id, "legacy:0")
                heading = html.escape(self.settings.header)
            if key in recovered:
                # Keep duplicate records visible to the publisher so all stale copies are hidden.
                key += ":" + str(message.id)
            recovered[key] = {"id": message.id, "hash": None}
            pages[key] = heading + "\n\nПродажи закрыты"
        if recovered:
            self.state.set("published", {"binding": self.binding(), "messages": recovered})
        return await self.publish(pages)

    async def publish(self, pages):
        async with self.lock:
            stored = self.state.get("published", {})
            manifest = stored.get("messages", {}) if stored.get("binding") == self.binding() else {}
            if not manifest or self.state.get("pending_publish"):
                await self._recover(pages, manifest)
            if "legacy:0" in manifest and pages and "legacy:0" not in pages:
                first_key = next(iter(pages))
                if first_key not in manifest:
                    manifest[first_key] = manifest.pop("legacy:0")
                    self.state.set("published", {"binding": self.binding(), "messages": manifest})
            # Read even on equal hashes: deleted posts must be restored.
            ids = [entry["id"] for entry in manifest.values()]
            current = await self.client.get_messages(self.target, ids=ids) if ids else []
            existing = {m.id: m for m in current if m and getattr(m, "message", None) is not None}
            changes = 0
            for key, content in pages.items():
                entry = manifest.get(key)
                target = existing.get(entry["id"]) if entry else None
                if target:
                    parsed, entities = telegram_html.parse(content)
                    if target.raw_text != parsed or list(target.entities or []) != list(entities or []):
                        try:
                            await self.client.edit_message(self.target, target.id, content,
                                                           parse_mode="html", link_preview=False)
                        except MessageNotModifiedError:
                            pass
                        changes += 1
                    target_id = target.id
                else:
                    # If send succeeds but the response is lost, recover this text next time.
                    self.state.set("pending_publish", {"binding": self.binding(), "key": key, "text": content})
                    message = await self.client.send_message(self.target, content,
                                                             parse_mode="html", link_preview=False)
                    target_id = message.id
                    changes += 1
                manifest[key] = {"id": target_id, "hash": digest(content)}
                self.state.update({
                    "published": {"binding": self.binding(), "messages": manifest},
                    "pending_publish": None,
                })
                if changes:
                    await asyncio.sleep(self.settings.send_delay)
            # Disabled/obsolete pages belong to this manifest only. Other posts are untouched.
            for key in list(manifest):
                if key not in pages:
                    await self.client.delete_messages(self.target, [manifest[key]["id"]])
                    del manifest[key]
                    self.state.set("published", {"binding": self.binding(), "messages": manifest})
                    changes += 1
                    await asyncio.sleep(self.settings.send_delay)
            return changes
