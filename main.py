from __future__ import annotations

import logging
import sys
from pathlib import Path

from services import SteamPresenceService
from sites.bloodycase import BloodyCaseSite
from sites.csgocases import CSGOCasesSite
from sites.keydrop import KeyDropSite
from sites.steam_playtime import SteamPlaytimeMonitor, format_hours_and_minutes

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
    bloodycase_session_file = SESSIONS_DIR / "bloodycase_session.json"
    csgocases_session_file = SESSIONS_DIR / "csgocases_session.json"
    session_file = SESSIONS_DIR / "session.json"
    steam_session_file = SESSIONS_DIR / "steam_session.json"
    balances_file = DATA_DIR / "balances.json"
    steam_playtime_file = DATA_DIR / "steam_playtime.json"
    bloodycase_steam_avatar_file = BASE_DIR / "images" / "bloodycase.png"
    keydrop_steam_avatar_file = BASE_DIR / "images" / "keydrop.webp"
    csgocases_steam_avatar_file = BASE_DIR / "images" / "csgocases.png"
    steam_presence_script = BASE_DIR / "cs2.js"

    logger.info("Comprobando requisito previo de horas recientes en Steam.")
    playtime_monitor = SteamPlaytimeMonitor(
        session_file=steam_session_file,
        workspace_dir=DATA_DIR,
        data_file=steam_playtime_file,
        logger=logging.getLogger("daily_cases_bot.steam"),
    )
    steam_presence = SteamPresenceService(
        script_path=steam_presence_script,
        logger=logging.getLogger("daily_cases_bot.steam_presence"),
    )

    logger.info("Inicializando bot para KeyDrop.")
    site = KeyDropSite(
        session_file=session_file,
        steam_session_file=steam_session_file,
        steam_avatar_file=keydrop_steam_avatar_file,
        steam_workspace_dir=DATA_DIR,
        balances_file=balances_file,
        logger=logging.getLogger("daily_cases_bot.keydrop"),
    )
    keydrop_result = "not_started"
    csgocases_result = "not_started"
    bloodycase_result = "not_started"

    try:
        recent_hours = playtime_monitor.check_recent_hours_once()
        if recent_hours < playtime_monitor.minimum_hours:
            logger.warning(
                "Counter-Strike 2 no cumple el requisito: %s / %s en las ultimas 2 semanas. "
                "Cierro el programa sin ejecutar KeyDrop.",
                format_hours_and_minutes(recent_hours),
                format_hours_and_minutes(playtime_monitor.minimum_hours),
            )
            return
        steam_presence.start()
        if not steam_presence.wait_until_ready():
            logger.warning(
                "Steam Presence no quedo listo. Revisa el login/2FA y vuelve a ejecutar."
            )
            return

        keydrop_result = site.run()
        logger.info("KeyDrop finalizo con estado: %s", keydrop_result)
        logger.info("Inicializando bot para CSGOCases.")
        csgocases_site = CSGOCasesSite(
            session_file=csgocases_session_file,
            steam_session_file=steam_session_file,
            steam_avatar_file=csgocases_steam_avatar_file,
            steam_workspace_dir=DATA_DIR,
            balances_file=balances_file,
            logger=logging.getLogger("daily_cases_bot.csgocases"),
        )
        csgocases_result = csgocases_site.run()
        logger.info("Inicializando bot para BloodyCase.")
        bloodycase_site = BloodyCaseSite(
            session_file=bloodycase_session_file,
            steam_session_file=steam_session_file,
            steam_avatar_file=bloodycase_steam_avatar_file,
            balances_file=balances_file,
            workspace_dir=DATA_DIR,
            logger=logging.getLogger("daily_cases_bot.bloodycase"),
        )
        bloodycase_result = bloodycase_site.run()
    except KeyboardInterrupt:
        logger.info("Ejecucion interrumpida por el usuario.")
    except Exception:
        logger.exception("Error no controlado en main.")
        try:
            input("El proceso sigue vivo. Pulsa Enter para cerrar.")
        except KeyboardInterrupt:
            logger.info("Cierre forzado por el usuario.")
    finally:
        try:
            steam_presence.stop()
        except KeyboardInterrupt:
            logger.info("Cierre interrumpido por el usuario mientras se detenia Steam Presence.")
        logger.info(
            "Resumen final | Steam: %s | KeyDrop: %s | CSGOCases: %s | BloodyCase: %s",
            format_hours_and_minutes(recent_hours)
            if "recent_hours" in locals()
            else "sin comprobar",
            keydrop_result,
            csgocases_result,
            bloodycase_result,
        )


if __name__ == "__main__":
    main()
