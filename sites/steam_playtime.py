from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .steam import STEAM_PROFILE_URL, SteamAvatarManager

STEAM_PROFILE_ENGLISH_URL = f"{STEAM_PROFILE_URL}?l=english"
STEAM_RECENT_GAMES_ENGLISH_URL = f"{STEAM_PROFILE_URL}/games/?tab=recent&l=english"
TARGET_GAME_NAME = "Counter-Strike 2"
TARGET_GAME_APP_ID = 730
TARGET_GAME_ALIASES = ("Counter-Strike 2", "Counter-Strike", "CS2", "CSGO")
RECENT_GAME_HOURS_PATTERN = re.compile(
    r"([0-9]+(?:[.,][0-9]+)?)\s*(?:hrs?|hours?)\s+last\s+two\s+weeks",
    re.IGNORECASE,
)


def save_playtime_snapshot(
    data_file: Path,
    recent_hours: float,
    minimum_hours: float,
    profile_url: str,
    game_name: str,
    app_id: int,
    logger: logging.Logger | None = None,
) -> bool:
    payload = {
        "recent_hours": recent_hours,
        "minimum_hours": minimum_hours,
        "profile_url": profile_url,
        "game_name": game_name,
        "app_id": app_id,
        "captured_at": datetime.now().astimezone().isoformat(),
    }

    try:
        data_file.parent.mkdir(parents=True, exist_ok=True)

        if data_file.exists():
            store = json.loads(data_file.read_text(encoding="utf-8"))
        else:
            store = {"latest": None, "history": []}

        store["latest"] = payload
        store["history"].append(payload)

        data_file.write_text(
            json.dumps(store, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        if logger:
            logger.exception("No se pudo guardar el snapshot de horas de Steam en %s", data_file)
        return False

    if logger:
        logger.info("Snapshot de horas de Steam guardado en %s.", data_file)
    return True


@dataclass(slots=True)
class SteamPlaytimeMonitor:
    session_file: Path
    workspace_dir: Path
    data_file: Path
    logger: logging.Logger
    minimum_hours: float = 5.0
    poll_interval_seconds: int = 300

    def check_recent_hours_once(self) -> float:
        steam_client = SteamAvatarManager(
            session_file=self.session_file,
            workspace_dir=self.workspace_dir,
            logger=self.logger,
        )
        steam_client.start()

        try:
            recent_hours = self.get_recent_hours(steam_client)
            save_playtime_snapshot(
                data_file=self.data_file,
                recent_hours=recent_hours,
                minimum_hours=self.minimum_hours,
                profile_url=STEAM_RECENT_GAMES_ENGLISH_URL,
                game_name=TARGET_GAME_NAME,
                app_id=TARGET_GAME_APP_ID,
                logger=self.logger,
            )
            return recent_hours
        finally:
            steam_client.close()

    def get_recent_hours(self, steam_client: SteamAvatarManager) -> float:
        page = steam_client.page
        if page is None:
            raise RuntimeError("No hay pagina activa de Steam para comprobar horas recientes.")

        page.goto(STEAM_RECENT_GAMES_ENGLISH_URL, wait_until="domcontentloaded")
        page.locator("body").wait_for(state="visible", timeout=5_000)
        steam_client.human_delay(0.2, 0.5)
        self.wait_for_recent_games_content(page)

        recent_hours = self.extract_target_game_recent_hours(page)
        self.logger.info(
            "Horas recientes detectadas para %s en Steam: %.1f",
            TARGET_GAME_NAME,
            recent_hours,
        )
        return recent_hours

    def wait_for_recent_games_content(self, page: Page) -> None:
        selectors = [
            ".gameListRow",
            ".gameListRowItemName",
            "#mainContents",
            "div.gameListOuterContainer",
        ]

        for selector in selectors:
            try:
                page.locator(selector).first.wait_for(state="visible", timeout=1_500)
                return
            except PlaywrightTimeoutError:
                continue

        self.logger.info("Steam no mostro filas recientes visibles de forma inmediata.")

    def extract_target_game_recent_hours(self, page: Page) -> float:
        rows: list[dict[str, Any]] = page.locator(".gameListRow").evaluate_all(
            """
            elements => elements.map(el => ({
                text: el.innerText || '',
                hrefs: Array.from(el.querySelectorAll('a[href]')).map(a => a.href),
                names: Array.from(el.querySelectorAll('.gameListRowItemName')).map(n => n.textContent || '')
            }))
            """
        )

        target_row = self.find_target_game_row(rows)
        if target_row is None:
            self.logger.info(
                "No se encontro fila reciente para %s. Se interpreta como 0.0 horas en las ultimas 2 semanas.",
                TARGET_GAME_NAME,
            )
            return 0.0

        row_text = str(target_row.get("text", ""))
        recent_match = RECENT_GAME_HOURS_PATTERN.search(row_text)
        if recent_match is None:
            self.write_debug_dump(page, rows)
            raise RuntimeError(
                f"No se pudo localizar el texto de horas recientes para {TARGET_GAME_NAME}."
            )

        recent_hours_text = recent_match.group(1).replace(",", ".")
        recent_hours = float(recent_hours_text)
        return recent_hours

    def find_target_game_row(self, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized_aliases = {self.normalize_game_name(alias) for alias in TARGET_GAME_ALIASES}

        for row in rows:
            hrefs = row.get("hrefs", [])
            if isinstance(hrefs, list) and any(f"/app/{TARGET_GAME_APP_ID}" in str(href) for href in hrefs):
                return row

        for row in rows:
            names = row.get("names", [])
            if not isinstance(names, list):
                continue
            for name in names:
                normalized_name = self.normalize_game_name(str(name))
                if normalized_name in normalized_aliases:
                    return row
                if "counterstrike" in normalized_name or "cs2" == normalized_name:
                    return row

        for row in rows:
            row_text = self.normalize_game_name(str(row.get("text", "")))
            if any(alias in row_text for alias in normalized_aliases):
                return row
            if "counterstrike" in row_text:
                return row

        return None

    def normalize_game_name(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def write_debug_dump(self, page: Page, rows: list[dict[str, Any]]) -> None:
        debug_dir = self.data_file.parent
        debug_dir.mkdir(parents=True, exist_ok=True)

        html_file = debug_dir / "steam_recent_games_debug.html"
        rows_file = debug_dir / "steam_recent_games_rows.json"
        body_file = debug_dir / "steam_recent_games_body.txt"

        try:
            html_file.write_text(page.content(), encoding="utf-8")
            body_file.write_text(page.locator("body").inner_text(timeout=10_000), encoding="utf-8")
            rows_file.write_text(
                json.dumps(rows, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.logger.warning(
                "Se genero dump de depuracion de Steam en %s, %s y %s.",
                html_file,
                rows_file,
                body_file,
            )
        except Exception:
            self.logger.exception("No se pudo generar el dump de depuracion de Steam.")
