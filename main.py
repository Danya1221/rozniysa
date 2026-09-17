import asyncio
import logging
import signal
from contextlib import suppress

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import Settings
from control_catalog import CatalogController as BotAPIController
from catalog_publisher import CatalogPublisher as PinnedBotAPIPublisher
from runtime import SyncService
from state import StateStore

log = logging.getLogger(__name__)


async def prepare_supplier(service, settings, controller=None):
    """Prepare one or two Telegram user accounts used to read supplier prices."""
    settings.validate(require_sync=False)
    if not settings.session:
        raise ValueError("Сессия аккаунта 1 не подключена; выполни /login 1 в управляющем боте")
    if not settings.sources:
        raise ValueError("Укажи SUPPLIER_BOT для чтения прайса")

    sessions = [(1, settings.session), (2, getattr(settings, "session_2", ""))]
    primary_ready = False
    for slot, session_string in sessions:
        if not session_string:
            continue
        try:
            try:
                session = StringSession(session_string)
            except Exception:
                raise ValueError(f"Сессия аккаунта {slot} повреждена; выполни /login {slot}") from None

            client = TelegramClient(
                session,
                settings.api_id,
                settings.api_hash,
                auto_reconnect=True,
                connection_retries=5,
                retry_delay=2,
                request_retries=3,
                flood_sleep_threshold=60,
            )
            service.attach_client(client, slot)
            ok = await service.connect_account(slot)
            if not ok:
                raise ValueError(service.account_errors.get(slot, f"Аккаунт {slot} не подключился"))

            me = await client.get_me()
            if me is None or me.bot:
                raise ValueError(f"Сессия аккаунта {slot} должна принадлежать пользовательскому Telegram-аккаунту")
            if controller is not None and slot == 1 and not settings.admin_ids:
                controller.admins = {me.id}

            saved = client.session.save()
            state_key = "session_string" if slot == 1 else "session_string_2"
            attr = "session" if slot == 1 else "session_2"
            if saved and service.state.get(state_key) != saved:
                service.state.set(state_key, saved)
            setattr(settings, attr, saved or session_string)

            for reader in service.readers_for_slot(slot):
                try:
                    await reader.resolve()
                except Exception as exc:
                    if slot == 1:
                        raise
                    log.warning(
                        "Аккаунт 2 не видит %s: %s: %s",
                        reader.source.label, type(exc).__name__, exc,
                    )
                    reader.entity = None

            if slot == 1:
                primary_ready = True
            log.info("Telegram-аккаунт %s подключён; источников: %s", slot, len(service.readers_for_slot(slot)))
        except Exception as exc:
            if slot == 1:
                raise
            service.account_errors[slot] = str(exc) or type(exc).__name__
            client = service.clients.get(slot)
            if client is not None:
                with suppress(Exception):
                    await client.disconnect()
            service.attach_client(None, slot)
            log.warning("Второй Telegram-аккаунт не подключён: %s", service.account_errors[slot])

    if not primary_ready:
        raise ValueError("Аккаунт 1 не подключён; выполни /login 1")
    service.startup_error = None
    service.ready = True


async def run_supplier(service, settings, controller=None, retry_seconds=30):
    """Run supplier sync forever and hot-reload either saved Telegram session."""
    database = getattr(service.state, "database", None)
    if database is not None:
        def waiting():
            service.startup_error = "Жду завершения предыдущего Railway deployment"
            log.warning("Жду PostgreSQL runtime lock перед подключением Telegram-сессии")

        await asyncio.to_thread(database.acquire_runtime_lock, waiting)
        log.info("PostgreSQL runtime lock получен")

    while not service.stop_event.is_set():
        service.ready = False
        service.reload_requested = False
        try:
            stored = service.state.get("session_string", "")
            stored_2 = service.state.get("session_string_2", "")
            if stored:
                settings.session = stored
            if stored_2:
                settings.session_2 = stored_2
            await asyncio.wait_for(prepare_supplier(service, settings, controller), timeout=180)
            await service.run()
            if service.stop_event.is_set():
                return
            if service.reload_requested:
                continue
            if service.startup_error:
                raise RuntimeError(service.startup_error)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            service.startup_error = str(exc) or type(exc).__name__
            service.ready = False
            log.error(
                "Чтение ботов-поставщиков недоступно: %s. Управляющий бот остаётся доступен",
                service.startup_error,
            )
        finally:
            await service.disconnect_clients()

        if service.reload_requested:
            continue
        service.wake.clear()
        wake_task = asyncio.create_task(service.wake.wait())
        stop_task = asyncio.create_task(service.stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {wake_task, stop_task}, timeout=retry_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if stop_task in done and service.stop_event.is_set():
                return
        finally:
            for task in (wake_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake_task, stop_task, return_exceptions=True)


async def main():
    settings = Settings.from_env(require_sync=False)
    state = StateStore(settings.state_file)
    state.acquire()

    stored_session = state.get("session_string", "")
    stored_session_2 = state.get("session_string_2", "")
    if stored_session:
        settings.session = stored_session
    if stored_session_2:
        settings.session_2 = stored_session_2

    controller = None
    control_task = None
    service = SyncService(None, settings, state)

    # Publishing is separate from supplier-bot reading. TARGET_CHANNEL is checked
    # only when the bot actually needs to publish/edit the finished price.
    if settings.bot_token:
        service.publisher = PinnedBotAPIPublisher(
            settings.bot_token,
            settings.target,
            state,
            settings,
        )

    try:
        # The control bot uses Telegram HTTP Bot API. This completely avoids
        # ImportBotAuthorizationRequest and its long MTProto FloodWait.
        if settings.bot_token:
            controller = BotAPIController(settings.bot_token, service, settings.admin_ids)
            try:
                await controller.start()
                control_task = asyncio.create_task(controller.run(), name="control-bot-api")
            except Exception as exc:
                log.exception("Управляющий бот не запущен через Bot API")
                print(
                    f"❌ Управляющий бот временно недоступен: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                with suppress(Exception):
                    await controller.close()
                controller = None
        else:
            log.warning(
                "BOT_TOKEN / CONTROL_BOT_TOKEN не задан: управляющий бот НЕ запущен. "
                "Добавь токен своего бота из BotFather в одну из этих переменных"
            )
            print("❌ Управляющий бот не запущен: нет BOT_TOKEN / CONTROL_BOT_TOKEN", flush=True)

        if state.database is not None:
            print("💾 State и пользовательская Telegram-сессия сохраняются в PostgreSQL", flush=True)
        else:
            print("⚠️ DATABASE_URL не задан: session хранится только в state.json", flush=True)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: (service.stop_event.set(), service.wake.set()))

        log.info("reprice1 запущен; подключаю ботов-поставщиков")
        supplier_task = asyncio.create_task(
            run_supplier(service, settings, controller), name="supplier-runtime"
        )
        stopper = asyncio.create_task(service.stop_event.wait(), name="stopper")

        watched = {supplier_task, stopper}
        if control_task is not None:
            watched.add(control_task)

        done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)

        if supplier_task in done:
            await supplier_task

        if control_task is not None and control_task in done:
            await control_task

    finally:
        service.stop_event.set()
        service.wake.set()

        if control_task is not None:
            control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)
        if controller is not None:
            with suppress(Exception):
                await controller.close()

        with suppress(Exception):
            await service.disconnect_clients()

        if hasattr(service.publisher, "close"):
            with suppress(Exception):
                await service.publisher.close()

        state.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
