from __future__ import annotations

from datetime import datetime
import json
import threading
from pathlib import Path
from typing import Any, Callable


class SteamStatusStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "recent_hours": None,
            "recent_hours_text": None,
            "avatar_path": None,
            "avatar_temporary": False,
            "profile_name": None,
            "profile_name_temporary": False,
            "presence_status": None,
            "presence_detail": None,
            "presence_ready": False,
            "profile_updated_at": None,
            "profile_refreshing": False,
        }
        self._store_file: Path | None = None
        self._callback: Callable[[dict[str, Any]], None] | None = None

    def configure_store(self, store_file: Path) -> None:
        with self._lock:
            self._store_file = store_file
            self._load_from_disk_locked()

    def set_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        with self._lock:
            self._callback = callback

    def update(self, **patch: Any) -> None:
        with self._lock:
            self._state.update(patch)
            self._persist_locked()
            snapshot = dict(self._state)
            callback = self._callback
        if callback is not None:
            callback(snapshot)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._store_file is not None:
                self._load_from_disk_locked()
            return dict(self._state)

    def _persist_locked(self) -> None:
        if self._store_file is None:
            return
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_from_disk_locked(self) -> None:
        if self._store_file is None or not self._store_file.exists():
            return
        try:
            payload = json.loads(self._store_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        self._state.update(payload)


_store = SteamStatusStore()


def configure_steam_status_store(store_file: Path) -> None:
    _store.configure_store(store_file)


def set_steam_status_callback(
    callback: Callable[[dict[str, Any]], None] | None,
) -> None:
    _store.set_callback(callback)


def update_steam_playtime(recent_hours: float, recent_hours_text: str) -> None:
    _store.update(
        recent_hours=recent_hours,
        recent_hours_text=recent_hours_text,
        profile_updated_at=datetime.now().astimezone().isoformat(),
        profile_refreshing=False,
    )


def update_steam_avatar(image_path: Path | str | None, temporary: bool) -> None:
    avatar_path = str(image_path) if image_path else None
    _store.update(
        avatar_path=avatar_path,
        avatar_temporary=temporary,
        profile_updated_at=datetime.now().astimezone().isoformat(),
        profile_refreshing=False,
    )


def update_steam_profile_name(name: str | None, temporary: bool) -> None:
    _store.update(
        profile_name=name,
        profile_name_temporary=temporary,
        profile_updated_at=datetime.now().astimezone().isoformat(),
        profile_refreshing=False,
    )


def update_steam_refreshing(refreshing: bool) -> None:
    _store.update(profile_refreshing=refreshing)


def update_steam_presence(
    status: str | None,
    detail: str | None = None,
    *,
    ready: bool | None = None,
) -> None:
    patch: dict[str, Any] = {
        "presence_status": status,
        "presence_detail": detail,
    }
    if ready is not None:
        patch["presence_ready"] = ready
    _store.update(**patch)


def get_steam_status_snapshot() -> dict[str, Any]:
    return _store.snapshot()
