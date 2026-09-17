"""Durable bot state. PostgreSQL is preferred; file storage remains a fallback."""
import copy
import json
import logging
import os
import tempfile
from pathlib import Path

from database import DatabaseStore

log = logging.getLogger(__name__)


class StateStore:
    DB_KEY = "rozniysa_state_v1"

    def __init__(self, path, database=None):
        self.path = Path(path)
        self.data = {}
        self._lock_file = None
        self.database = database
        self._owns_database = False
        if self.database is None:
            url = os.getenv("DATABASE_URL", "").strip()
            if url:
                self.database = DatabaseStore(url)
                self._owns_database = True
        self.load()

    def load(self):
        if self.database is not None:
            payload = self.database.get(self.DB_KEY)
            if payload:
                raw = json.loads(payload)
                if not isinstance(raw, dict):
                    raise RuntimeError("В PostgreSQL сохранён некорректный state")
                self.data = raw
                log.info("State загружен из PostgreSQL")
                return
            # One-time migration from a local state file when it exists.
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self.data = raw
                except Exception:
                    pass
            self.save()
            log.info("State создан в PostgreSQL")
            return

        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Ожидался JSON-объект")
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Не удалось прочитать {self.path}; восстанови state.json из копии") from exc
        self.data = raw

    def acquire(self):
        # PostgreSQL has its own process-safe runtime lock. The local flock is
        # only needed for the filesystem fallback.
        if self.database is not None:
            return
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.path) + ".lock", "a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("На этом диске уже запущен экземпляр reprice1") from exc
        self._lock_file = handle

    def close(self):
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None
        if self._owns_database and self.database is not None:
            self.database.close()
            self.database = None

    def save(self):
        if self.database is not None:
            self.database.set(self.DB_KEY, json.dumps(self.data, ensure_ascii=False, separators=(",", ":")))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def update(self, values):
        previous = copy.deepcopy(self.data)
        self.data.update(copy.deepcopy(values))
        try:
            self.save()
        except Exception:
            self.data = previous
            raise

    def set(self, key, value):
        self.update({key: value})
