"""Ordered price posts and a persistent two-column navigation message."""
import asyncio
import html
import json
import re

from bot_publisher import digest, is_missing_message_error
from first_message_publisher import PinnedBotAPIPublisher


CATALOG_TEXT = "🗂 КАТАЛОГ — выбери модель или категорию\nНажми на кнопку — перейдёшь к нужной части прайса.\nСкопируй позицию вместе с ценой и пришли её менеджеру."


def message_link(chat_id, message_id, username=""):
    if username:
        return f"https://t.me/{username.lstrip('@')}/{message_id}"
    marked = str(chat_id)
    if marked.startswith("-100"):
        return f"https://t.me/c/{marked[4:]}/{message_id}"
    return None


def page_title(content):
    match = re.match(r"<b>\s*(?:—\s*)?(.*?)(?:\s*—)?\s*</b>", content)
    return html.unescape(match[1]) if match else "Прайс"


def iphone_catalog_group(title):
    """One catalog button per physical iPhone generation message."""
    name = re.sub(r"\s+", " ", title).casefold().strip()
    if not name.startswith("iphone"):
        return ""
    if re.search(r"\b(?:11|12|13|14|15)\b", name):
        return "iPhone 11–15"
    if re.search(r"\b16(?:e)?\b", name):
        return "iPhone 16"
    if "air" in name or re.search(r"\b17(?:e)?\b", name):
        return "iPhone 17"
    return "iPhone"


def catalog_labels(content):
    """Use exactly the visible physical Telegram post heading for navigation."""
    title = re.sub(r"\s+", " ", page_title(content)).strip() or "Прайс"
    # Telegram inline button text is bounded; keep the beginning intact because
    # it is the same text the customer sees at the top of the price message.
    if len(title) > 64:
        title = title[:61].rstrip() + "…"
    return [title]


def iphone_model_label(raw):
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw.casefold() == "iphone air":
        return "iPhone Air"
    match = re.fullmatch(r"iPhone\s+(\d{1,2}e?)(?:\s+(Plus|Pro(?:\s+Max)?))?", raw, re.I)
    if not match:
        return ""
    result = "iPhone " + match.group(1)
    suffix = match.group(2)
    if suffix:
        suffix = re.sub(r"\s+", " ", suffix).title().replace("Pro Max", "Pro Max")
        result += " " + suffix
    return result


def catalog_group(title):
    """Map physical post headings to a small public navigation set."""
    name = title.casefold().strip()
    if name.startswith("iphone"):
        return iphone_catalog_group(title)
    if name.startswith(("apple watch", "airpods", "ipad", "macbook", "mac mini", "mac studio", "apple tv", "apple", "cpo", "asis")):
        return "Apple"
    if name.startswith("samsung"):
        return "Samsung"
    if name.startswith(("honor", "realme", "huawei", "tecno", "xiaomi", "google", "oneplus", "oppo", "vivo", "nothing", "nubia", "infinix")):
        return "Смартфоны"
    if name.startswith(("oura", "coros", "garmin", "ray-ban")):
        return "Часы / носимое"
    if name.startswith(("bowers", "harman", "bose", "marshall", "jbl", "anker", "rode")):
        return "Аудио"
    if name.startswith(("dji", "insta360", "gopro", "kodak", "fujifilm", "canon", "sony")):
        return "Фото / видео"
    if name.startswith(("nintendo", "playstation", "xbox")):
        return "Игры"
    return "Другое"


class CatalogPublisher(PinnedBotAPIPublisher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.layout_lock = asyncio.Lock()

    def arrange_manifest(self, pages, manifest):
        # Telegram cannot move posts. Reuse their chronological slots, then
        # update the catalog from the final key -> message ID mapping.
        slots = sorted(manifest.values(), key=lambda entry: int(entry["id"]))
        arranged = {key: entry for key, entry in zip(pages, slots)}
        for entry in slots[len(pages):]:
            arranged["obsolete:" + str(entry["id"])] = entry
        return arranged

    def catalog_records(self):
        stored = self.state.get("catalog", {}) or {}
        return stored.get("messages", []) if stored.get("binding") == self.binding() else []

    def save_catalog(self, records):
        self.state.set("catalog", {"binding": self.binding(), "messages": records})

    async def _before_recreate_first(self):
        """Remove navigation first, then prices, before recreating the intro.

        That guarantees the physical Telegram order remains:
        first message -> price blocks -> catalog.
        """
        records = self.catalog_records()
        for record in list(records):
            if not record.get("id"):
                continue
            try:
                await self._delete(record["id"])
            except RuntimeError as exc:
                if not is_missing_message_error(exc):
                    raise
        if records:
            self.save_catalog([])
        return await super()._before_recreate_first()

    async def _catalog_entry(self, records, index, text, keyboard):
        record = records[index] if index < len(records) else {}
        payload = {"chat_id": self.target, "text": text,
                   "reply_markup": {"inline_keyboard": keyboard}, "disable_web_page_preview": True}
        content_hash = digest(json.dumps([text, keyboard], ensure_ascii=False, sort_keys=True))
        changed = 0
        if record.get("id") and record.get("hash") != content_hash:
            try:
                await self.api("editMessageText", message_id=record["id"], **payload)
                changed = 1
            except RuntimeError as exc:
                error = str(exc).lower()
                if is_missing_message_error(exc):
                    record = {}
                elif "message is not modified" not in error:
                    raise
        if not record.get("id"):
            message = await self.api("sendMessage", **payload)
            record = {"id": int(message["message_id"])}
            changed = 1
        record.update({"hash": content_hash, "text": text})
        if index == len(records):
            records.append(record)
        else:
            records[index] = record
        self.save_catalog(records)
        if changed:
            await asyncio.sleep(max(0, self.settings.send_delay))
        return changed

    async def _prepare_catalog(self, count):
        """Ensure catalog messages exist strictly after every managed price post."""
        records = self.catalog_records()
        stored = self.state.get("published", {}) or {}
        manifest = stored.get("messages", {}) if stored.get("binding") == self.binding() else {}
        price_ids = [int(entry["id"]) for entry in manifest.values() if entry.get("id")]
        catalog_ids = [int(record["id"]) for record in records if record.get("id")]

        # Old versions pinned the catalog and sometimes reused the first price slot.
        # Recreate such records once so the custom intro remains the only pin and
        # the catalog becomes physically the last managed message.
        must_rebuild = bool(records) and (
            any(record.get("pinned") for record in records)
            or (price_ids and catalog_ids and min(catalog_ids) <= max(price_ids))
        )
        if must_rebuild:
            for record in list(records):
                try:
                    await self._delete(record["id"])
                except RuntimeError as exc:
                    if not is_missing_message_error(exc):
                        raise
            records = []
            self.save_catalog(records)

        for index in range(count):
            if index >= len(records) or not records[index].get("hash"):
                await self._catalog_entry(records, index, CATALOG_TEXT + "\n\nОбновляю разделы…", [])
        return records

    async def _update_catalog(self, pages, records):
        manifest = self.state.get("published", {}).get("messages", {})
        buttons = []
        seen_titles = set()
        for key, content in pages.items():
            if key not in manifest:
                continue
            link = message_link(self.target, manifest[key]["id"])
            if not link:
                continue
            for title in catalog_labels(content):
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                buttons.append({"text": title, "url": link})

        # Preserve the exact physical post order. The catalog is navigation to
        # those posts, so it must not reorder or rename them independently.
        batches = [buttons[start:start + 80] for start in range(0, len(buttons), 80)] or [[]]
        changes = 0

        # Create any extra catalog pages before adding inter-page links so every
        # referenced message id already exists.
        while len(records) < len(batches):
            changes += await self._catalog_entry(
                records, len(records), CATALOG_TEXT + "\n\nОбновляю разделы…", []
            )

        for index, batch in enumerate(batches):
            rows = [batch[start:start + 2] for start in range(0, len(batch), 2)]
            text = CATALOG_TEXT + (f"\nСтраница {index + 1} из {len(batches)}" if len(batches) > 1 else "")
            if not pages:
                text += "\n\nПока нет выбранных позиций."
            elif not batch:
                text += "\n\nСсылки на посты доступны в каналах и супергруппах."
            if index + 1 < len(batches):
                link = message_link(self.target, records[index + 1]["id"])
                if link:
                    rows.append([{"text": "Следующие разделы →", "url": link}])
            changes += await self._catalog_entry(records, index, text, rows)
        for index in range(len(records) - 1, len(batches) - 1, -1):
            try:
                await self._delete(records[index]["id"])
            except RuntimeError as exc:
                if not is_missing_message_error(exc):
                    raise
            records.pop(index)
            self.save_catalog(records)
        return changes

    async def _repair_first_message_layout_once(self):
        text = str(self.state.get("first_message_text", "") or "").strip()
        if not text or self.state.get("first_message_layout_version", 0) >= 2:
            return

        records = self.catalog_records()
        first = self.state.get("first_message", {}) or {}
        ids = []
        if first.get("id"):
            ids.append(int(first["id"]))
        ids.extend(int(record["id"]) for record in records if record.get("id"))
        for message_id in dict.fromkeys(ids):
            try:
                await self._delete(message_id)
            except RuntimeError as exc:
                if not is_missing_message_error(exc):
                    raise
        self.save_catalog([])
        self.state.set("first_message", {})
        await self._clear_managed_price_posts()
        self.state.set("first_message_layout_version", 2)

    async def publish(self, pages):
        async with self.layout_lock:
            await self.ensure_target()
            await self._repair_first_message_layout_once()
            await self._ensure_first_message()
            # First publish/reorder every price page. Only then create or move the
            # navigation catalog, otherwise Telegram places it before later posts.
            changes = await super().publish(pages)
            count = 1
            records = await self._prepare_catalog(count)

            # Existing Telegram catalog messages may carry a hash produced by an
            # older button-layout algorithm.  Force one in-place keyboard rewrite
            # when this layout version changes; keep the same message ID.
            force_catalog = int(self.state.get("catalog_layout_version", 0) or 0) < 5
            if force_catalog:
                for record in records:
                    record["hash"] = ""
                self.save_catalog(records)

            changes += await self._update_catalog(pages, records)
            if force_catalog:
                self.state.set("catalog_layout_version", 5)
            return changes

    async def set_first_message(self, text):
        async with self.layout_lock:
            await self.ensure_target()
            previous = self.state.get("first_message", {}) or {}
            if not previous.get("id") or previous.get("binding") != self.binding():
                # Establish a newly requested intro ahead of the rebuilt catalog.
                records = self.catalog_records()
                for record in list(records):
                    try:
                        await self._delete(record["id"])
                    except RuntimeError as exc:
                        if "message to delete not found" not in str(exc).lower():
                            raise
                    records.remove(record)
                    self.save_catalog(records)
            changes = await super().set_first_message(text)
            self.state.set("first_message_layout_version", 2)
            return changes
