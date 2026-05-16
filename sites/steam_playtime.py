from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from steam_status import update_steam_playtime

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
RECENT_GAME_MINUTES_PATTERN = re.compile(
    r"([0-9]+)\s*minutes?\s+last\s+two\s+weeks",
    re.IGNORECASE,
)
EMBEDDED_TARGET_GAME_PATTERN = re.compile(
    r'\{\\"appid\\":730,\\"name\\":\\"Counter-Strike 2\\",(?P<body>.*?)}',
    re.DOTALL,
)
EMBEDDED_LAST_PLAYED_PATTERN = re.compile(r'\\"rtime_last_played\\":(?P<value>\d+)')
EMBEDDED_PLAYTIME_FOREVER_PATTERN = re.compile(r'\\"playtime_forever\\":(?P<value>\d+)')


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


def format_hours_and_minutes(hours: float) -> str:
    total_minutes = max(0, round(hours * 60))
    precise_hours = total_minutes / 60
    return f"{precise_hours:.2f} h ({total_minutes} min)"


@dataclass(slots=True)
class SteamPlaytimeMonitor:
    session_file: Path
    workspace_dir: Path
    data_file: Path
    logger: logging.Logger
    minimum_hours: float = 5.0
    poll_interval_seconds: int = 300
    headless: bool = False
    slow_mo_ms: int = 90

    def check_recent_hours_once(self) -> float:
        steam_client = SteamAvatarManager(
            session_file=self.session_file,
            workspace_dir=self.workspace_dir,
            logger=self.logger,
            headless=self.headless,
            slow_mo_ms=self.slow_mo_ms,
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
            update_steam_playtime(
                recent_hours,
                format_hours_and_minutes(recent_hours),
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
            "Horas recientes detectadas para %s en Steam: %s",
            TARGET_GAME_NAME,
            format_hours_and_minutes(recent_hours),
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
        if target_row is not None:
            row_text = str(target_row.get("text", ""))
            recent_hours = self.parse_recent_hours_from_text(row_text)
            if recent_hours is not None:
                return recent_hours

        body_text = page.locator("body").inner_text(timeout=10_000)
        recent_hours = self.extract_target_game_recent_hours_from_body(body_text)
        if recent_hours is not None:
            return recent_hours

        embedded_metadata = self.extract_target_game_metadata_from_html(page.content())
        if embedded_metadata is not None:
            self.log_embedded_target_game_metadata(embedded_metadata)

        self.logger.info(
            "No se encontro actividad reciente visible para %s. Se interpreta como 0.0 horas en las ultimas 2 semanas.",
            TARGET_GAME_NAME,
        )
        return 0.0

    def find_target_game_row(self, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized_aliases = {self.normalize_game_name(alias) for alias in TARGET_GAME_ALIASES}

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

        for row in rows:
            hrefs = row.get("hrefs", [])
            if not isinstance(hrefs, list):
                continue
            if any(f"/app/{TARGET_GAME_APP_ID}" in str(href) for href in hrefs):
                row_text = str(row.get("text", ""))
                if self.contains_target_game_alias(row_text):
                    return row

        return None

    def normalize_game_name(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def contains_target_game_alias(self, value: str) -> bool:
        normalized_value = self.normalize_game_name(value)
        normalized_aliases = [self.normalize_game_name(alias) for alias in TARGET_GAME_ALIASES]
        return any(alias in normalized_value for alias in normalized_aliases) or "counterstrike" in normalized_value

    def parse_recent_hours_from_text(self, value: str) -> float | None:
        hours_match = RECENT_GAME_HOURS_PATTERN.search(value)
        if hours_match is not None:
            return float(hours_match.group(1).replace(",", "."))

        minutes_match = RECENT_GAME_MINUTES_PATTERN.search(value)
        if minutes_match is not None:
            minutes = int(minutes_match.group(1))
            return round(minutes / 60, 2)

        normalized = " ".join(value.split())
        compact_hours_match = re.search(
            r"last two weeks\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:hrs?|hours?)",
            normalized,
            re.IGNORECASE,
        )
        if compact_hours_match is not None:
            return float(compact_hours_match.group(1).replace(",", "."))

        compact_minutes_match = re.search(
            r"last two weeks\s*([0-9]+)\s*minutes?",
            normalized,
            re.IGNORECASE,
        )
        if compact_minutes_match is not None:
            minutes = int(compact_minutes_match.group(1))
            return round(minutes / 60, 2)

        return None

    def extract_target_game_recent_hours_from_body(self, body_text: str) -> float | None:
        lines = [line.strip() for line in body_text.replace("\r", "").split("\n")]
        non_empty_lines = [line for line in lines if line]

        for index, line in enumerate(non_empty_lines):
            if not self.is_target_game_line(line):
                continue

            block = "\n".join(non_empty_lines[index : index + 8])
            recent_hours = self.parse_recent_hours_from_text(block)
            if recent_hours is not None:
                return recent_hours

            if index + 2 < len(non_empty_lines):
                header_line = non_empty_lines[index + 1]
                value_line = non_empty_lines[index + 2]
                if header_line.upper() == "LAST TWO WEEKS":
                    recent_hours = self.parse_recent_hours_from_text(
                        f"last two weeks {value_line}"
                    )
                    if recent_hours is not None:
                        return recent_hours

        return None

    def is_target_game_line(self, value: str) -> bool:
        normalized_value = self.normalize_game_name(value)
        normalized_aliases = {self.normalize_game_name(alias) for alias in TARGET_GAME_ALIASES}
        return normalized_value in normalized_aliases or normalized_value == "counterstrike2"

    def extract_target_game_metadata_from_html(self, html: str) -> dict[str, int] | None:
        match = EMBEDDED_TARGET_GAME_PATTERN.search(html)
        if match is None:
            return None

        body = match.group("body")
        metadata: dict[str, int] = {}

        last_played_match = EMBEDDED_LAST_PLAYED_PATTERN.search(body)
        if last_played_match is not None:
            metadata["rtime_last_played"] = int(last_played_match.group("value"))

        playtime_forever_match = EMBEDDED_PLAYTIME_FOREVER_PATTERN.search(body)
        if playtime_forever_match is not None:
            metadata["playtime_forever"] = int(playtime_forever_match.group("value"))

        return metadata or None

    def log_embedded_target_game_metadata(self, metadata: dict[str, int]) -> None:
        last_played_timestamp = metadata.get("rtime_last_played")
        playtime_forever_minutes = metadata.get("playtime_forever")

        details: list[str] = []
        if last_played_timestamp is not None:
            last_played = datetime.fromtimestamp(
                last_played_timestamp, tz=timezone.utc
            ).astimezone()
            details.append(f"ultimo uso segun Steam: {last_played.isoformat()}")

        if playtime_forever_minutes is not None:
            total_hours = round(playtime_forever_minutes / 60, 1)
            details.append(f"tiempo total acumulado: {total_hours:.1f} h")

        if details:
            self.logger.info(
                "Steam embebe datos de %s aunque no aparezca en recientes: %s.",
                TARGET_GAME_NAME,
                " | ".join(details),
            )

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
