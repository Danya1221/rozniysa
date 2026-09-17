"""Entry point for the retail price bot; isolated from reprice1 state/sessions."""
import asyncio
import logging
import os
import re
import signal
from contextlib import suppress

from config import Settings
from main import run_supplier
from retail_control import RetailController
from retail_publisher import RetailPublisher
from retail_runtime import RetailSyncService
from retail_store import RetailStore, ProcessLease
from state import StateStore


async def main():
    settings = Settings.from_env(require_sync=False)
    username = os.getenv("ORDER_BOT_USERNAME", "").strip().lstrip("@")
    if not settings.bot_token or not settings.admin_ids:
        raise ValueError("Задай BOT_TOKEN и ADMIN_IDS")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("Задай ORDER_BOT_USERNAME: username бота оформления")
    url = os.getenv("RETAIL_DATABASE_URL", "").strip()
    retail = await asyncio.to_thread(RetailStore, url)
    if not os.getenv("DATABASE_URL") and not retail.sqlite:
        os.environ["DATABASE_URL"] = url
    if not os.getenv("STATE_FILE"):
        settings.state_file = "rozniysa_state.json"
    lease = ProcessLease(retail, "rozniysa")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    while not stop.is_set() and not await asyncio.to_thread(lease.acquire):
        logging.info("Жду завершения предыдущего процесса розничного прайса")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), 3)
    if stop.is_set():
        lease.close()
        return
    state, controller, service, tasks = None, None, None, []
    try:
        # Acquire before loading state so overlapping deployments never use stale IDs.
        state = StateStore(settings.state_file)
        state.acquire()
        service = RetailSyncService(None, settings, state, retail, username)
        service.stop_event = stop
        service.publisher = RetailPublisher(settings.bot_token, settings.target, state, settings, retail)
        controller = RetailController(settings.bot_token, service, settings.admin_ids)
        await controller.start()
        await asyncio.to_thread(retail.mark_uncertain, "Подключение к поставщикам")

        async def watch_lock():
            while not stop.is_set():
                await asyncio.to_thread(lease.check)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), 5)

        tasks = [asyncio.create_task(controller.run()), asyncio.create_task(run_supplier(service, settings, controller)),
                 asyncio.create_task(watch_lock()), asyncio.create_task(stop.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if controller:
            await controller.close()
        if service:
            await service.disconnect_clients()
            await service.publisher.close()
        if state:
            state.close()
        lease.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
