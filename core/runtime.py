from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    base_dir: Path
    sessions_dir: Path
    logs_dir: Path
    data_dir: Path
    log_file: Path
    db_file: Path
    settings_file: Path
    steam_state_file: Path
    balances_file: Path
    steam_playtime_file: Path
    steam_presence_script: Path
    steam_session_file: Path
    keydrop_session_file: Path
    csgocases_session_file: Path
    bloodycase_session_file: Path
    cs2free_session_file: Path
    keydrop_steam_avatar_file: Path
    csgocases_steam_avatar_file: Path
    bloodycase_steam_avatar_file: Path

    @classmethod
    def from_base_dir(cls, base_dir: Path) -> "RuntimePaths":
        base_dir = base_dir.resolve()
        sessions_dir = base_dir / "sessions"
        logs_dir = base_dir / "logs"
        data_dir = base_dir / "data"
        images_dir = base_dir / "images"
        return cls(
            base_dir=base_dir,
            sessions_dir=sessions_dir,
            logs_dir=logs_dir,
            data_dir=data_dir,
            log_file=logs_dir / "bot.log",
            db_file=data_dir / "app.db",
            settings_file=data_dir / "settings.json",
            steam_state_file=data_dir / "steam_state.json",
            balances_file=data_dir / "balances.json",
            steam_playtime_file=data_dir / "steam_playtime.json",
            steam_presence_script=base_dir / "cs2.js",
            steam_session_file=sessions_dir / "steam_session.json",
            keydrop_session_file=sessions_dir / "session.json",
            csgocases_session_file=sessions_dir / "csgocases_session.json",
            bloodycase_session_file=sessions_dir / "bloodycase_session.json",
            cs2free_session_file=sessions_dir / "cs2free_session.json",
            keydrop_steam_avatar_file=images_dir / "keydrop.webp",
            csgocases_steam_avatar_file=images_dir / "csgocases.png",
            bloodycase_steam_avatar_file=images_dir / "bloodycase.png",
        )


def ensure_runtime_dirs(paths: RuntimePaths) -> None:
    for directory in (paths.sessions_dir, paths.logs_dir, paths.data_dir):
        directory.mkdir(parents=True, exist_ok=True)


def configure_logging(
    log_file: Path,
    extra_handlers: list[logging.Handler] | None = None,
) -> logging.Logger:
    logger = logging.getLogger("daily_cases_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.set_name("daily_cases_console")
    console_handler.setFormatter(formatter)
    _attach_handler_once(logger, console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.set_name("daily_cases_file")
    file_handler.setFormatter(formatter)
    _attach_handler_once(logger, file_handler)

    for handler in extra_handlers or []:
        if handler.formatter is None:
            handler.setFormatter(formatter)
        _attach_handler_once(logger, handler)

    return logger


def _attach_handler_once(logger: logging.Logger, handler: logging.Handler) -> None:
    for existing in logger.handlers:
        if existing is handler:
            return
        existing_name = getattr(existing, "name", None)
        handler_name = getattr(handler, "name", None)
        if existing_name and handler_name and existing_name == handler_name:
            return
        if (
            isinstance(existing, logging.FileHandler)
            and isinstance(handler, logging.FileHandler)
            and getattr(existing, "baseFilename", None)
            == getattr(handler, "baseFilename", None)
        ):
            return
        if (
            type(existing) is type(handler)
            and isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            and getattr(existing, "stream", None) is getattr(handler, "stream", None)
        ):
            return

    logger.addHandler(handler)
