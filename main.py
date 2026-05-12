from __future__ import annotations

import logging
import sys
from pathlib import Path

from sites.keydrop import KeyDropSite
from sites.steam_playtime import SteamPlaytimeMonitor

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
LOG_FILE = LOGS_DIR / "bot.log"


def ensure_runtime_dirs() -> None:
    for directory in (SESSIONS_DIR, LOGS_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("daily_cases_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def main() -> None:
    ensure_runtime_dirs()
    logger = configure_logging()
    session_file = SESSIONS_DIR / "session.json"
    steam_session_file = SESSIONS_DIR / "steam_session.json"
    balances_file = DATA_DIR / "balances.json"
    steam_playtime_file = DATA_DIR / "steam_playtime.json"
    steam_avatar_file = BASE_DIR / "images" / "keydrop.webp"

    logger.info("Comprobando requisito previo de horas recientes en Steam.")
    playtime_monitor = SteamPlaytimeMonitor(
        session_file=steam_session_file,
        workspace_dir=DATA_DIR,
        data_file=steam_playtime_file,
        logger=logging.getLogger("daily_cases_bot.steam"),
    )

    logger.info("Inicializando bot para KeyDrop.")
    site = KeyDropSite(
        session_file=session_file,
        steam_session_file=steam_session_file,
        steam_avatar_file=steam_avatar_file,
        steam_workspace_dir=DATA_DIR,
        balances_file=balances_file,
        logger=logging.getLogger("daily_cases_bot.keydrop"),
    )

    try:
        recent_hours = playtime_monitor.check_recent_hours_once()
        if recent_hours < playtime_monitor.minimum_hours:
            logger.warning(
                "Counter-Strike 2 no cumple el requisito: %.1f / %.1f horas en las ultimas 2 semanas. "
                "Cierro el programa sin ejecutar KeyDrop.",
                recent_hours,
                playtime_monitor.minimum_hours,
            )
            return
        site.run()
    except KeyboardInterrupt:
        logger.info("Ejecucion interrumpida por el usuario.")
    except Exception:
        logger.exception("Error no controlado en main.")
        input("El proceso sigue vivo. Pulsa Enter para cerrar.")


if __name__ == "__main__":
    main()
