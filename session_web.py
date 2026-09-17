"""Temporary, protected browser login. No Telegram client is created at import time."""
import asyncio
import hmac
import html
import os
import secrets
import time

from aiohttp import web
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from config import env_int

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Подключение Telegram</title>
<style>body{font-family:system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 20px}
input,button{font:inherit;padding:12px;margin:6px 0;width:100%;box-sizing:border-box}
button{cursor:pointer;background:#ff8500;color:#fff;border:0;border-radius:10px}
.card{border:1px solid #ddd;border-radius:14px;padding:20px;margin:16px 0}
.err{color:#b42318}pre{white-space:pre-wrap;word-break:break-all}</style></head>
<body><h1>Подключение Telegram</h1>__BODY__</body></html>"""


def esc(value):
    return html.escape(str(value or ""), quote=True)


def page(body, status=200):
    return web.Response(text=PAGE.replace("__BODY__", body), content_type="text/html", status=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"})


def create_app(api_id=None, api_hash=None, setup_key=None):
    api_id = api_id if api_id is not None else env_int("API_ID", 0, maximum=2**31-1)
    api_hash = api_hash if api_hash is not None else os.getenv("API_HASH", "").strip()
    setup_key = setup_key if setup_key is not None else os.getenv("SETUP_KEY", "").strip()
    if not api_id or not api_hash:
        raise ValueError("Заполни API_ID и API_HASH")
    if not setup_key:
        raise ValueError("Добавь SETUP_KEY для защиты страницы подключения")
    state = {"client": None, "flow": None, "phone": None, "hash": None,
             "stage": "phone", "last_code": 0.0}
    lock = asyncio.Lock()

    def form(stage, flow, note=""):
        fields = {
            "phone": '<input name="phone" placeholder="+31612345678" autocomplete="tel" required>',
            "code": '<input name="code" placeholder="Код из Telegram" autocomplete="one-time-code" required>',
            "password": '<input name="password" type="password" placeholder="Облачный пароль Telegram" autocomplete="off" required>',
        }
        labels = {"phone": "Отправить код", "code": "Подтвердить", "password": "Войти"}
        return (f'<div class="card"><p>{esc(note)}</p><form method="post" action="/{stage}">'
                f'<input type="hidden" name="csrf" value="{esc(flow)}">'
                '<input name="key" type="password" placeholder="SETUP_KEY" autocomplete="off" required>'
                + fields[stage] + f'<button type="submit">{labels[stage]}</button></form></div>')

    def valid_key(value):
        return hmac.compare_digest(value.encode(), setup_key.encode())

    async def check(request):
        data = await request.post()
        if not valid_key(str(data.get("key", ""))):
            raise web.HTTPForbidden(text="Неверный SETUP_KEY", headers={"Cache-Control": "no-store"})
        flow = request.cookies.get("setup_flow", "")
        if not flow or not hmac.compare_digest(flow, str(data.get("csrf", ""))):
            raise web.HTTPForbidden(text="Открой страницу заново")
        return data, flow

    async def disconnect():
        if state["client"]:
            await state["client"].disconnect()
            state["client"] = None

    async def index(request):
        flow = secrets.token_urlsafe(32)
        response = page(form("phone", flow, "Введи SETUP_KEY и номер аккаунта, которому доступны прайсы."))
        response.set_cookie("setup_flow", flow, httponly=True, samesite="Strict",
                            secure=request.secure or request.headers.get("X-Forwarded-Proto") == "https",
                            max_age=900)
        return response

    async def phone(request):
        data, flow = await check(request)
        async with lock:
            if time.monotonic() - state["last_code"] < 30:
                return page(form("phone", flow, "Подожди 30 секунд перед повторным запросом"), 429)
            await disconnect()
            state.update(flow=flow, phone=str(data.get("phone", "")).strip(), hash=None, stage="phone")
            state["client"] = TelegramClient(StringSession(), api_id, api_hash, flood_sleep_threshold=0)
            try:
                await state["client"].connect()
                state["last_code"] = time.monotonic()
                sent = await state["client"].send_code_request(state["phone"])
                state.update(hash=sent.phone_code_hash, stage="code")
                return page(form("code", flow, "Код отправлен. Проверь сообщение от Telegram."))
            except errors.FloodWaitError as exc:
                return page(form("phone", flow, f"Telegram просит подождать {exc.seconds} секунд"), 429)
            except Exception:
                await disconnect()
                return page(form("phone", flow, "Не удалось отправить код. Проверь номер, API_ID и API_HASH."), 400)

    async def show_session():
        client = state["client"]
        if not await client.is_user_authorized():
            raise RuntimeError("Авторизация не завершена")
        value = client.session.save()
        state.update(stage="done", phone=None, hash=None, flow=None)
        # Disconnect, never log_out: logging out would revoke the string just generated.
        await disconnect()
        return page('<div class="card"><h2>Готово ✅</h2>'
                    '<p>Скопируй строку в Railway → Variables → SESSION_STRING:</p>'
                    f'<pre>{esc(value)}</pre>'
                    '<p>Поставь SETUP_MODE=false, удали SETUP_KEY и выполни Redeploy.</p></div>')

    async def code(request):
        data, flow = await check(request)
        async with lock:
            if flow != state["flow"] or state["stage"] != "code":
                return page(form("phone", flow, "Сначала запроси новый код"), 400)
            try:
                await state["client"].sign_in(phone=state["phone"],
                    code="".join(str(data.get("code", "")).split()), phone_code_hash=state["hash"])
                return await show_session()
            except errors.SessionPasswordNeededError:
                state["stage"] = "password"
                return page(form("password", flow, "Введи пароль двухэтапной аутентификации."))
            except errors.PhoneCodeExpiredError:
                state["stage"] = "phone"
                return page(form("phone", flow, "Код истёк. Запроси новый."), 400)
            except errors.PhoneCodeInvalidError:
                return page(form("code", flow, "Неверный код. Попробуй ещё раз."), 400)
            except errors.FloodWaitError as exc:
                return page(form("code", flow, f"Подожди {exc.seconds} секунд"), 429)
            except Exception:
                return page(form("phone", flow, "Не удалось завершить вход. Запроси код заново."), 400)

    async def password(request):
        data, flow = await check(request)
        async with lock:
            if flow != state["flow"] or state["stage"] != "password":
                return page(form("phone", flow, "Сначала подтверди номер и код"), 400)
            try:
                await state["client"].sign_in(password=str(data.get("password", "")))
                return await show_session()
            except errors.PasswordHashInvalidError:
                return page(form("password", flow, "Неверный облачный пароль. Попробуй ещё раз."), 400)
            except errors.FloodWaitError as exc:
                return page(form("password", flow, f"Подожди {exc.seconds} секунд"), 429)
            except Exception:
                return page(form("phone", flow, "Не удалось завершить вход. Начни заново."), 400)

    async def cleanup(app):
        await disconnect()

    app = web.Application(client_max_size=16 * 1024)
    app.add_routes([web.get("/", index), web.post("/phone", phone),
                    web.post("/code", code), web.post("/password", password)])
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=env_int("PORT", 8080, 1, 65535), access_log=None)
