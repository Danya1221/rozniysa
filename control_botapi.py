"""Control bot over Telegram Bot API.

The control bot must not use an MTProto bot session: repeated Railway deploys can
trigger ImportBotAuthorizationRequest FloodWait. Supplier/user access still uses
Telethon separately.
"""
import asyncio
import hashlib
import logging
import re
from contextlib import suppress

import aiohttp
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from config import decimal_value
from prices import marked_price

log = logging.getLogger(__name__)


class BotAPIController:
    def __init__(self, token, service, admins):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.service = service
        self.admins = set(admins)
        self.username = ""
        self.http = None
        self.offset = 0
        self.task = None
        self.block_choices = {}
        self.login_flows = {}
        self.closed = False

    async def api(self, method, **payload):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        async with self.http.post(f"{self.base}/{method}", json=payload) as response:
            data = await response.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(f"Bot API {method}: {data.get('description', data)}")
        return data.get("result")

    async def start(self):
        me = await self.api("getMe")
        self.username = (me.get("username") or "").lower()
        # Long polling and webhooks are mutually exclusive.
        with suppress(Exception):
            await self.api("deleteWebhook", drop_pending_updates=False)
        log.info("Управляющий бот @%s готов через Bot API", self.username)
        print(f"🤖 Управляющий бот @{self.username} готов через Bot API — /start должен отвечать", flush=True)

    def menu(self):
        return {"inline_keyboard": [
            [{"text": "▶️ Запустить", "callback_data": "resume"}, {"text": "⏸ Остановить", "callback_data": "pause"}],
            [{"text": "🔄 Запросить сейчас", "callback_data": "sync"}, {"text": "📊 Статус", "callback_data": "status"}],
            [{"text": "👥 Telegram-аккаунты", "callback_data": "accounts"}],
            [{"text": "⏱ Интервал", "callback_data": "interval"}, {"text": "💰 Наценка", "callback_data": "markup"}],
            [{"text": "📦 Блоки", "callback_data": "blocks:0"}, {"text": "📱 SIM / eSIM", "callback_data": "sim"}],
        ]}

    async def send(self, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.api("sendMessage", **payload)

    async def answer_callback(self, callback_id, text=None, alert=False):
        payload = {"callback_query_id": callback_id, "show_alert": alert}
        if text:
            payload["text"] = text[:180]
        with suppress(Exception):
            await self.api("answerCallbackQuery", **payload)

    def allowed(self, user_id, chat_type="private"):
        return chat_type == "private" and user_id in self.admins

    async def explain_access(self, chat_id, user_id, chat_type):
        if chat_type != "private":
            markup = None
            if self.username:
                markup = {"inline_keyboard": [[{"text": "Открыть бота", "url": f"https://t.me/{self.username}?start=menu"}]]}
            await self.send(chat_id, "Управление прайсом работает в личных сообщениях с ботом. Открой бота и отправь /start.", markup)
            return
        await self.send(
            chat_id,
            "Бот работает, но для этого аккаунта ещё не настроен доступ.\n"
            f"Твой Telegram ID: {user_id}\n"
            "Добавь этот ID в ADMIN_IDS (или ADMIN_ID) в Railway и сделай Redeploy.\n"
            "После этого отправь /login, чтобы войти в Telegram-аккаунт, которому доступны прайсы.",
        )

    async def _close_login(self, user_id):
        flow = self.login_flows.pop(user_id, None)
        client = flow.get("client") if flow else None
        if client is not None:
            with suppress(Exception):
                await client.disconnect()

    async def accounts_menu(self, chat_id):
        connected_1 = bool(1 in getattr(self.service, "clients", {}) and self.service.ready)
        connected_2 = bool(2 in getattr(self.service, "clients", {}))
        saved_1 = bool(self.service.state.get("session_string", "") or self.service.settings.session)
        saved_2 = bool(self.service.state.get("session_string_2", "") or getattr(self.service.settings, "session_2", ""))
        text = (
            "👥 Telegram-аккаунты поставщиков\n\n"
            f"1. {'🟢 подключён' if connected_1 else ('🟡 сохранён' if saved_1 else '⚪ не подключён')}\n"
            f"2. {'🟢 подключён' if connected_2 else ('🟡 сохранён' if saved_2 else '⚪ не подключён')}\n\n"
            "Аккаунт 1 читает все источники. Аккаунт 2 читает только HI (Поставщик 1). "
            "Если цена HI отличается, в итоговый прайс попадёт меньшая закупочная цена."
        )
        await self.send(chat_id, text, {"inline_keyboard": [
            [{"text": "🔑 Войти в аккаунт 1", "callback_data": "account:login:1"}],
            [{"text": "🔑 Войти в аккаунт 2", "callback_data": "account:login:2"}],
        ]})

    async def begin_login(self, chat_id, user_id, slot=1):
        slot = int(slot)
        if slot not in {1, 2}:
            raise ValueError("Аккаунт может быть только 1 или 2")
        await self._close_login(user_id)
        self.login_flows[user_id] = {"stage": "phone", "client": None, "chat_id": chat_id, "slot": slot}
        await self.send(chat_id, f"🔐 Вход в Telegram-аккаунт {slot}\n\nОтправь номер телефона в международном формате, например:\n+79991234567\n\nДля отмены: /cancel")

    async def finish_login(self, chat_id, user_id, flow):
        slot = int(flow.get("slot", 1))
        session_string = flow["client"].session.save()
        state_key = "session_string" if slot == 1 else "session_string_2"
        attr = "session" if slot == 1 else "session_2"
        self.service.state.set(state_key, session_string)
        setattr(self.service.settings, attr, session_string)
        self.service.startup_error = None
        if hasattr(self.service, "account_errors"):
            self.service.account_errors.pop(slot, None)
        if hasattr(self.service, "request_reconnect"):
            self.service.request_reconnect()
        await self._close_login(user_id)
        await self.send(chat_id, f"✅ Аккаунт {slot} подключён, сессия сохранена в базе.\nПереподключаю чтение поставщиков автоматически.\n\nПосле подключения нажми «🔄 Запросить сейчас».", self.menu())

    async def handle_login_input(self, chat_id, user_id, text):
        flow = self.login_flows.get(user_id)
        if not flow:
            return False
        try:
            if flow["stage"] == "phone":
                phone = re.sub(r"[\s()\-]", "", text)
                if not re.fullmatch(r"\+\d{7,15}", phone):
                    await self.send(chat_id, "Номер нужен в формате +79991234567. Попробуй ещё раз или /cancel")
                    return True
                temp = TelegramClient(StringSession(), self.service.settings.api_id, self.service.settings.api_hash,
                                      auto_reconnect=False, connection_retries=2, request_retries=2,
                                      flood_sleep_threshold=0)
                await temp.connect()
                sent = await temp.send_code_request(phone)
                flow.update(stage="code", client=temp, phone=phone, phone_code_hash=sent.phone_code_hash)
                await self.send(chat_id, "📩 Код отправлен Telegram. Пришли код сюда цифрами.\nДля отмены: /cancel")
                return True
            if flow["stage"] == "code":
                code = re.sub(r"\D", "", text)
                if not code:
                    await self.send(chat_id, "Пришли код только цифрами или /cancel")
                    return True
                try:
                    await flow["client"].sign_in(phone=flow["phone"], code=code,
                                                 phone_code_hash=flow["phone_code_hash"])
                except SessionPasswordNeededError:
                    flow["stage"] = "password"
                    await self.send(chat_id, "🔑 На аккаунте включена 2FA. Отправь облачный пароль.\nДля отмены: /cancel")
                    return True
                await self.finish_login(chat_id, user_id, flow)
                return True
            if flow["stage"] == "password":
                await flow["client"].sign_in(password=text)
                await self.finish_login(chat_id, user_id, flow)
                return True
        except PhoneNumberInvalidError:
            await self._close_login(user_id)
            await self.send(chat_id, "❌ Telegram не принял номер. Отправь /login и введи номер заново.")
        except PhoneCodeInvalidError:
            await self.send(chat_id, "❌ Неверный код. Пришли правильный код ещё раз или /cancel")
        except PhoneCodeExpiredError:
            await self._close_login(user_id)
            await self.send(chat_id, "❌ Код истёк. Отправь /login, чтобы получить новый.")
        except PasswordHashInvalidError:
            await self.send(chat_id, "❌ Неверный пароль 2FA. Попробуй ещё раз или /cancel")
        except FloodWaitError as exc:
            await self._close_login(user_id)
            await self.send(chat_id, f"⏳ Telegram просит подождать {exc.seconds} сек. Потом повтори /login")
        except Exception as exc:
            log.exception("Ошибка входа через управляющего бота")
            await self._close_login(user_id)
            await self.send(chat_id, f"❌ Вход не завершён: {type(exc).__name__}: {exc}\nПовтори /login")
        return True

    async def background_sync(self, chat_id):
        try:
            result = await self.service.sync(force=True)
            await self.send(chat_id, result, self.menu())
        except Exception:
            log.exception("Ошибка ручного обновления")
            await self.send(chat_id, "Ошибка обновления. Проверь /status и журнал Railway", self.menu())

    async def request_sync(self, chat_id):
        if self.service.busy or (self.task and not self.task.done()):
            await self.send(chat_id, "Обновление уже выполняется")
            return
        await self.send(chat_id, "Запрашиваю прайс…")
        self.task = asyncio.create_task(self.background_sync(chat_id))

    async def show_blocks(self, chat_id, page=0):
        blocks = sorted({i.block for i in self.service.cached_items(include_closed=True)})
        block_id = lambda block: hashlib.sha256(block.encode()).hexdigest()[:16]
        self.block_choices = {block_id(block): block for block in blocks}
        disabled = set(self.service.options().get("disabled_blocks", []))
        page = max(0, min(page, max(0, (len(blocks) - 1) // 8)))
        rows = []
        for n, block in enumerate(blocks):
            if page * 8 <= n < (page + 1) * 8:
                rows.append([{"text": ("☑️ " if block not in disabled else "⬜ ") + block,
                              "callback_data": f"toggle:{block_id(block)}:{page}"}])
        nav = []
        if page:
            nav.append({"text": "←", "callback_data": f"blocks:{page-1}"})
        if (page + 1) * 8 < len(blocks):
            nav.append({"text": "→", "callback_data": f"blocks:{page+1}"})
        if nav:
            rows.append(nav)
        await self.send(chat_id, "Выбери блоки для своего прайса" if blocks else "Сначала запроси прайс",
                        {"inline_keyboard": rows} if rows else None)

    async def handle_callback(self, callback):
        user_id = callback.get("from", {}).get("id")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type", "private")
        if not self.allowed(user_id, chat_type):
            await self.answer_callback(callback["id"], f"Нет доступа. Твой ID: {user_id}", True)
            return
        await self.answer_callback(callback["id"])
        data = callback.get("data") or ""
        try:
            if data == "resume":
                self.service.set_option("enabled", True)
                self.service.last_attempt = 0
                await self.send(chat_id, "Синхронизация включена", self.menu())
            elif data == "pause":
                await self.service.pause()
                await self.send(chat_id, "Синхронизация остановлена", self.menu())
            elif data == "sync":
                await self.request_sync(chat_id)
            elif data == "status":
                await self.send(chat_id, self.service.status(), self.menu())
            elif data == "accounts":
                await self.accounts_menu(chat_id)
            elif data.startswith("account:login:"):
                await self.begin_login(chat_id, user_id, int(data.rsplit(":", 1)[1]))
            elif data == "markup":
                await self.send(chat_id, "Отправь /markup 500 — фиксированная наценка.\n/percent 5 — наценка 5%.\nМожно применять вместе.")
            elif data == "interval":
                await self.send(chat_id, "Интервал обновления:", {"inline_keyboard": [
                    [{"text": "5 мин.", "callback_data": "every:5"}, {"text": "15 мин.", "callback_data": "every:15"}, {"text": "30 мин.", "callback_data": "every:30"}],
                    [{"text": "1 час", "callback_data": "every:60"}, {"text": "2 часа", "callback_data": "every:120"}],
                ]})
            elif data.startswith("every:"):
                minutes = int(data.split(":", 1)[1])
                if minutes not in {5, 15, 30, 60, 120}:
                    raise ValueError("Недопустимый интервал")
                self.service.set_option("poll_seconds", minutes * 60)
                await self.send(chat_id, f"Интервал: {minutes} мин.", self.menu())
            elif data == "sim":
                await self.send(chat_id, "Фильтр iPhone; страна не используется для угадывания SIM:", {"inline_keyboard": [
                    [{"text": "Все", "callback_data": "sim:all"}, {"text": "SIM", "callback_data": "sim:sim"}],
                    [{"text": "eSIM", "callback_data": "sim:esim"}, {"text": "2 SIM", "callback_data": "sim:dual"}],
                    [{"text": "Не указан", "callback_data": "sim:unknown"}],
                ]})
            elif data.startswith("sim:"):
                choice = data.split(":", 1)[1]
                if choice not in {"all", "sim", "esim", "dual", "unknown"}:
                    raise ValueError("Неизвестный фильтр")
                self.service.set_option("sim_filter", choice)
                await self.service.refresh_format()
                await self.send(chat_id, "Фильтр применён: " + choice, self.menu())
            elif data.startswith("blocks:"):
                await self.show_blocks(chat_id, int(data.split(":", 1)[1]))
            elif data.startswith("toggle:"):
                _, number, page = data.split(":")
                block = self.block_choices.get(number)
                if not block:
                    await self.show_blocks(chat_id)
                    return
                disabled = set(self.service.options().get("disabled_blocks", []))
                disabled.symmetric_difference_update({block})
                self.service.set_option("disabled_blocks", sorted(disabled))
                await self.service.refresh_format()
                await self.show_blocks(chat_id, int(page))
        except Exception as exc:
            log.exception("Ошибка callback")
            await self.send(chat_id, "Не удалось применить: " + str(exc))

    async def handle_message(self, message):
        text = (message.get("text") or "").strip()
        if not text:
            return
        user_id = message.get("from", {}).get("id")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type", "private")
        words = text.split(maxsplit=1)
        command, _, recipient = words[0].lower().partition("@")
        if recipient and self.username and recipient != self.username:
            return

        if user_id in self.login_flows and not command.startswith("/"):
            if not self.allowed(user_id, chat_type):
                await self.explain_access(chat_id, user_id, chat_type)
                return
            await self.handle_login_input(chat_id, user_id, text)
            return

        commands = {"/start", "/help", "/id", "/status", "/sync", "/stop", "/resume", "/markup",
                    "/percent", "/interval", "/order", "/rejected", "/login", "/login2", "/accounts", "/cancel"}
        if command not in commands:
            return
        if command == "/id" and chat_type == "private":
            await self.send(chat_id, f"Твой Telegram ID: {user_id}")
            return
        if not self.allowed(user_id, chat_type):
            await self.explain_access(chat_id, user_id, chat_type)
            return
        value = words[1] if len(words) > 1 else ""
        try:
            if command in {"/start", "/help"}:
                text_out = ("🛠 Управление прайсом\n\n/login 1 — подключить аккаунт 1\n/login 2 — подключить аккаунт 2\n/accounts — состояние аккаунтов\n"
                            "/markup 500 — наценка\n/percent 5 — процент\n/interval 15 — интервал в минутах\n"
                            "/order iPhone 17, Samsung, Dyson — порядок блоков\n/rejected — нераспознанные строки\n"
                            "/id — твой Telegram ID\n/status /sync /stop /resume")
                if not self.service.ready:
                    text_out += "\n\n⚠️ " + self.service.startup_status()
                await self.send(chat_id, text_out, self.menu())
            elif command in {"/login", "/login2"}:
                requested = "2" if command == "/login2" else (value.strip() or "1")
                if requested not in {"1", "2"}:
                    raise ValueError("Используй /login 1 или /login 2")
                await self.begin_login(chat_id, user_id, int(requested))
            elif command == "/accounts":
                await self.accounts_menu(chat_id)
            elif command == "/cancel":
                if user_id in self.login_flows:
                    await self._close_login(user_id)
                    await self.send(chat_id, "Вход отменён", self.menu())
                else:
                    await self.send(chat_id, "Сейчас нет активного входа", self.menu())
            elif command == "/status":
                await self.send(chat_id, self.service.status(), self.menu())
            elif command == "/sync":
                await self.request_sync(chat_id)
            elif command == "/stop":
                await self.service.pause()
                await self.send(chat_id, "Синхронизация остановлена", self.menu())
            elif command == "/resume":
                self.service.set_option("enabled", True)
                self.service.last_attempt = 0
                await self.send(chat_id, "Синхронизация включена", self.menu())
            elif command in {"/markup", "/percent"}:
                number = decimal_value(value)
                if command == "/percent" and number <= -100:
                    raise ValueError("Процент должен быть больше -100")
                option = "markup" if command == "/markup" else "markup_percent"
                proposed = {**self.service.options(), option: str(number)}
                for item in self.service.cached_items(include_closed=True):
                    marked_price(item, self.service.settings, proposed)
                self.service.set_option(option, str(number))
                await self.service.refresh_format()
                await self.send(chat_id, "Наценка применена", self.menu())
            elif command == "/interval":
                minutes = int(value)
                if not 1 <= minutes <= 1440:
                    raise ValueError("Интервал: от 1 до 1440 минут")
                self.service.set_option("poll_seconds", minutes * 60)
                await self.send(chat_id, f"Интервал: {minutes} мин.", self.menu())
            elif command == "/order":
                order = [b.strip() for b in value.split(",") if b.strip()]
                self.service.set_option("block_order", order)
                await self.send(chat_id, "Порядок сохранён. Для уже опубликованных сообщений применяется при пересоздании блоков.")
            elif command == "/rejected":
                lines = []
                for source in self.service.state.get("sources", {}).values():
                    lines.extend(source.get("rejected", []))
                await self.send(chat_id, "\n".join(lines)[:3500] or "Нераспознанных строк нет")
        except (ValueError, ArithmeticError) as exc:
            await self.send(chat_id, str(exc))
        except Exception:
            log.exception("Ошибка управления")
            await self.send(chat_id, "Настройка сохранена, но обновление не завершено. Проверь /status")

    async def run(self):
        while not self.closed and not self.service.stop_event.is_set():
            try:
                updates = await self.api("getUpdates", offset=self.offset, timeout=25,
                                         allowed_updates=["message", "callback_query"])
                for update in updates:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    if "callback_query" in update:
                        await self.handle_callback(update["callback_query"])
                    elif "message" in update:
                        await self.handle_message(update["message"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Bot API polling error: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(3)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        for user_id in list(self.login_flows):
            await self._close_login(user_id)
        if self.http is not None and not self.http.closed:
            await self.http.close()
