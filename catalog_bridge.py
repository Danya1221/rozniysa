"""Send public price snapshots to checkout without accessing its database."""
import json
from http.client import HTTPException
import logging
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

log = logging.getLogger(__name__)


class CatalogBridgeError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Never forward the catalog secret to a redirected origin.
        return None


def validate_config(url, key):
    try:
        value = urlsplit(url)
        valid = (value.scheme == "https" or (value.scheme == "http" and value.hostname in {"127.0.0.1", "localhost", "::1"}))
        valid = valid and bool(value.hostname) and value.path in {"", "/"}
        valid = valid and not (value.username or value.password or value.query or value.fragment)
        _ = value.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("CATALOG_API_URL: укажи HTTPS-домен сервиса zayavki, без пути /api и параметров")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key or ""):
        raise ValueError("SYNC_API_KEY: одинаковый секрет из 32–128 латинских букв, цифр, _ или - в обоих проектах")


def http_error_detail(exc):
    """Read a small authenticated API error body without exposing secrets."""
    try:
        payload = json.loads(exc.read(4096).decode("utf-8", "replace"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail") or payload.get("error") or ""
    return str(detail).strip()[:500]


class CatalogBridge:
    def __init__(self, local_store, url, key, *, timeout=25, attempts=3):
        validate_config(url, key)
        self.local = local_store
        self.url = url.rstrip("/") + "/api/catalog/sync"
        self._key = key
        self.timeout, self.attempts = timeout, attempts
        self._lock = threading.RLock()

    def _payload(self, operation, **values):
        # Persist before transmission; restores and concurrent callers cannot
        # generate a revision older than an already issued local update.
        with self.local.transaction() as tx:
            revision = max(time.time_ns() // 1000, tx.get("bridge", "revision", 0) + 1)
            tx.set("bridge", "revision", revision)
        return {"protocol": 1, "revision": revision, "operation": operation, **values}

    def _error(self, message):
        self.local.set("bridge", "status", {"ok": False, "message": message, "attempted_at": time.time()})
        return CatalogBridgeError(message)

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        request = Request(self.url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
        opener = build_opener(NoRedirect())
        for attempt in range(self.attempts):
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    result = json.loads(response.read(2048))
                if not isinstance(result, dict) or result.get("ok") is not True or result.get("protocol") != 1 or result.get("revision") != payload["revision"]:
                    raise ValueError("Unexpected catalog response")
                self.local.set("bridge", "status", {"ok": True, "attempted_at": time.time(), "message": "Каталог передан в оформление"})
                return result
            except HTTPError as exc:
                detail = http_error_detail(exc)
                if exc.code in {401, 403}:
                    raise self._error("Нет доступа к API оформления. Проверь одинаковый SYNC_API_KEY") from None
                if exc.code == 409:
                    raise self._error("API оформления уже принял более новое обновление. Запроси прайс ещё раз") from None
                if exc.code < 500 and exc.code != 429:
                    suffix = f": {detail}" if detail else ""
                    raise self._error(f"API оформления отклонил каталог (HTTP {exc.code}){suffix}") from None
            except (URLError, OSError, TimeoutError, HTTPException, ValueError):
                pass
            if attempt + 1 < self.attempts:
                time.sleep(attempt + 1)
        raise self._error("Бот оформления недоступен. Новый прайс не опубликован; повтор при следующем обновлении")

    def put_catalog(self, products, *, confirmed, checked_at=None):
        with self._lock:
            values = {"products": products, "confirmed": bool(confirmed),
                      "checked_at": time.time() if checked_at is None else checked_at}
            url = self.local.get("bridge", "catalog_url", "")
            if url:
                values["catalog_url"] = url
            result = self._send(self._payload("snapshot", **values))
            return result.get("cancelled_drafts", 0)

    def mark_uncertain(self, reason):
        with self._lock:
            try:
                self._send(self._payload("uncertain", reason=str(reason)[:300]))
                return True
            except CatalogBridgeError:
                # Control/login remains available if checkout is temporarily down.
                # Its saved catalog expires using its ORIGINAL supplier timestamp.
                log.warning("Не удалось передать статус в оформление; актуальность ограничена сроком прайса")
                return False

    def set(self, namespace, key, value):
        if (namespace, key) != ("system", "catalog_url"):
            raise ValueError("API допускает только публикацию ссылки каталога")
        with self._lock:
            self.local.set("bridge", "catalog_url", value)
            try:
                self._send(self._payload("catalog_url", url=value))
                return True
            except CatalogBridgeError:
                # The URL is also included in the next complete price snapshot.
                log.warning("Ссылка каталога сохранена локально и будет передана повторно")
                return False
