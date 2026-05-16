from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from interaction import ask_text
from services import SteamPresenceService
from sites.bloodycase import BloodyCaseSite
from sites.cs2free import CS2FreeSite
from sites.csgocases import CSGOCasesSite
from sites.keydrop import KeyDropSite
from sites.steam import SteamAvatarManager
from sites.steam_playtime import SteamPlaytimeMonitor, format_hours_and_minutes

from .history import HistoryStore
from .models import ExecutionSummary, SiteExecutionRecord
from .runtime import RuntimePaths


class DailyCasesRunner:
    def __init__(
        self,
        paths: RuntimePaths,
        logger: logging.Logger,
        history_store: HistoryStore | None = None,
        progress_callback: Callable[[list[dict[str, str]]], None] | None = None,
    ) -> None:
        self.paths = paths
        self.logger = logger
        self.history_store = history_store
        self.progress_callback = progress_callback

    def run(self) -> ExecutionSummary:
        started_at = datetime.now().astimezone()
        playtime_monitor = SteamPlaytimeMonitor(
            session_file=self.paths.steam_session_file,
            workspace_dir=self.paths.data_dir,
            data_file=self.paths.steam_playtime_file,
            logger=logging.getLogger("daily_cases_bot.steam"),
        )
        steam_presence = SteamPresenceService(
            script_path=self.paths.steam_presence_script,
            logger=logging.getLogger("daily_cases_bot.steam_presence"),
        )

        recent_hours: float | None = None
        keydrop_result = "not_started"
        csgocases_result = "not_started"
        bloodycase_result = "not_started"
        cs2free_result = "not_started"
        run_status = "completed"
        progress_rows = self.build_initial_progress_rows()
        self.emit_progress(progress_rows)

        self.logger.info("Comprobando requisito previo de horas recientes en Steam.")
        self.logger.info("Inicializando bot para KeyDrop.")

        try:
            recent_hours = playtime_monitor.check_recent_hours_once()
            self.capture_initial_steam_profile_snapshot()
            if recent_hours < playtime_monitor.minimum_hours:
                self.logger.warning(
                    "Counter-Strike 2 no cumple el requisito: %s / %s en las ultimas 2 semanas. "
                    "Cierro el programa sin ejecutar KeyDrop.",
                    format_hours_and_minutes(recent_hours),
                    format_hours_and_minutes(playtime_monitor.minimum_hours),
                )
                run_status = "blocked_by_steam_hours"
            else:
                steam_presence.start()
                if not steam_presence.wait_until_ready():
                    self.logger.warning(
                        "Steam Presence no quedo listo. Revisa el login/2FA y vuelve a ejecutar."
                    )
                    run_status = "steam_presence_not_ready"
                else:
                    self.mark_site_in_progress(progress_rows, "keydrop")
                    keydrop_result = self.build_keydrop_site().run()
                    self.mark_site_completed(progress_rows, "keydrop", keydrop_result)
                    self.logger.info("KeyDrop finalizo con estado: %s", keydrop_result)
                    self.logger.info("Inicializando bot para CSGOCases.")
                    self.mark_site_in_progress(progress_rows, "csgocases")
                    csgocases_result = self.build_csgocases_site().run()
                    self.mark_site_completed(progress_rows, "csgocases", csgocases_result)
                    self.logger.info("Inicializando bot para BloodyCase.")
                    self.mark_site_in_progress(progress_rows, "bloodycase")
                    bloodycase_result = self.build_bloodycase_site().run()
                    self.mark_site_completed(progress_rows, "bloodycase", bloodycase_result)
                    self.logger.info("Inicializando bot para CS2.free.")
                    self.mark_site_in_progress(progress_rows, "cs2free")
                    cs2free_result = self.build_cs2free_site().run()
                    self.mark_site_completed(progress_rows, "cs2free", cs2free_result)
        except KeyboardInterrupt:
            run_status = "interrupted"
            self.logger.info("Ejecucion interrumpida por el usuario.")
        except Exception:
            run_status = "failed"
            self.logger.exception("Error no controlado en main.")
            try:
                ask_text(
                    "El proceso sigue vivo. Pulsa Enter para cerrar.",
                    title="Error en la ejecucion",
                )
            except KeyboardInterrupt:
                self.logger.info("Cierre forzado por el usuario.")
        finally:
            try:
                steam_presence.stop()
            except KeyboardInterrupt:
                self.logger.info(
                    "Cierre interrumpido por el usuario mientras se detenia Steam Presence."
                )
            self.emit_progress(progress_rows)
            self.logger.info(
                "Resumen final | Steam: %s | KeyDrop: %s | CSGOCases: %s | BloodyCase: %s | CS2.free: %s",
                format_hours_and_minutes(recent_hours)
                if recent_hours is not None
                else "sin comprobar",
                keydrop_result,
                csgocases_result,
                bloodycase_result,
                cs2free_result,
            )

        finished_at = datetime.now().astimezone()
        site_results = self.collect_site_results(
            started_at=started_at,
            finished_at=finished_at,
            site_statuses={
                "keydrop": keydrop_result,
                "csgocases": csgocases_result,
                "bloodycase": bloodycase_result,
                "cs2free": cs2free_result,
            },
        )
        total_balance_value = self.calculate_total_balance(site_results)
        summary = ExecutionSummary(
            started_at=started_at,
            finished_at=finished_at,
            recent_hours=recent_hours,
            run_status=run_status,
            site_results=site_results,
            total_balance_value=total_balance_value,
        )
        if self.history_store is not None:
            self.history_store.record_execution(summary)
        return summary

    def capture_initial_steam_profile_snapshot(self) -> None:
        steam_manager = SteamAvatarManager(
            session_file=self.paths.steam_session_file,
            workspace_dir=self.paths.data_dir,
            logger=logging.getLogger("daily_cases_bot.steam"),
        )
        try:
            steam_manager.start()
            steam_manager.backup_current_avatar()
            steam_manager.backup_current_profile_name()
        except Exception:
            self.logger.exception(
                "No se pudo capturar el snapshot inicial del perfil de Steam."
            )
        finally:
            try:
                steam_manager.close()
            except Exception:
                self.logger.exception(
                    "No se pudo cerrar SteamAvatarManager tras capturar el snapshot inicial de Steam."
                )

    def build_initial_progress_rows(self) -> list[dict[str, str]]:
        return [
            {"site_name": "keydrop", "phase": "Pendiente", "result": "-"},
            {"site_name": "csgocases", "phase": "Pendiente", "result": "-"},
            {"site_name": "bloodycase", "phase": "Pendiente", "result": "-"},
            {"site_name": "cs2free", "phase": "Pendiente", "result": "-"},
        ]

    def mark_site_in_progress(
        self,
        progress_rows: list[dict[str, str]],
        site_name: str,
    ) -> None:
        for row in progress_rows:
            if row["site_name"] == site_name:
                row["phase"] = "En curso"
                row["result"] = "-"
                break
        self.emit_progress(progress_rows)

    def mark_site_completed(
        self,
        progress_rows: list[dict[str, str]],
        site_name: str,
        result: str,
    ) -> None:
        for row in progress_rows:
            if row["site_name"] == site_name:
                row["phase"] = "Hecho"
                row["result"] = result
                break
        self.emit_progress(progress_rows)

    def emit_progress(self, progress_rows: list[dict[str, str]]) -> None:
        if self.progress_callback is None:
            return
        self.progress_callback([dict(row) for row in progress_rows])

    def build_keydrop_site(self) -> KeyDropSite:
        return KeyDropSite(
            session_file=self.paths.keydrop_session_file,
            steam_session_file=self.paths.steam_session_file,
            steam_avatar_file=self.paths.keydrop_steam_avatar_file,
            steam_workspace_dir=self.paths.data_dir,
            balances_file=self.paths.balances_file,
            logger=logging.getLogger("daily_cases_bot.keydrop"),
        )

    def build_csgocases_site(self) -> CSGOCasesSite:
        return CSGOCasesSite(
            session_file=self.paths.csgocases_session_file,
            steam_session_file=self.paths.steam_session_file,
            steam_avatar_file=self.paths.csgocases_steam_avatar_file,
            steam_workspace_dir=self.paths.data_dir,
            balances_file=self.paths.balances_file,
            logger=logging.getLogger("daily_cases_bot.csgocases"),
        )

    def build_bloodycase_site(self) -> BloodyCaseSite:
        return BloodyCaseSite(
            session_file=self.paths.bloodycase_session_file,
            steam_session_file=self.paths.steam_session_file,
            steam_avatar_file=self.paths.bloodycase_steam_avatar_file,
            balances_file=self.paths.balances_file,
            workspace_dir=self.paths.data_dir,
            logger=logging.getLogger("daily_cases_bot.bloodycase"),
        )

    def build_cs2free_site(self) -> CS2FreeSite:
        return CS2FreeSite(
            session_file=self.paths.cs2free_session_file,
            workspace_dir=self.paths.data_dir,
            logger=logging.getLogger("daily_cases_bot.cs2free"),
        )

    def collect_site_results(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        site_statuses: dict[str, str],
    ) -> list[SiteExecutionRecord]:
        diagnostics_config = {
            "keydrop": ("keydrop_daily_case", "keydrop_*.json"),
            "bloodycase": ("bloodycase_daily_free", "bloodycase_*.json"),
            "cs2free": ("cs2free_daily", "cs2free_*.json"),
        }
        balances_store = self.load_balances_store()
        results: list[SiteExecutionRecord] = []

        for site_name in ("keydrop", "csgocases", "bloodycase", "cs2free"):
            balance_payload = self.read_latest_balance_payload(balances_store, site_name)
            diagnostic_payload: dict[str, Any] | None = None
            diagnostic_path: Path | None = None

            if site_name in diagnostics_config:
                subdir, pattern = diagnostics_config[site_name]
                diagnostic_path, diagnostic_payload = self.find_latest_diagnostic_payload(
                    self.paths.data_dir / subdir,
                    pattern,
                    started_at,
                    finished_at,
                )

            results.append(
                SiteExecutionRecord(
                    site_name=site_name,
                    status=site_statuses.get(site_name, "not_started"),
                    balance_text=self.read_text_field(
                        balance_payload,
                        "balance_text",
                    )
                    or self.read_text_field(
                        diagnostic_payload,
                        "balance_text_after",
                    )
                    or self.read_text_field(
                        diagnostic_payload,
                        "balance_text_before",
                    ),
                    balance_value=self.read_float_field(balance_payload, "balance_value")
                    if balance_payload
                    else self.read_float_field(diagnostic_payload, "balance_value_after")
                    or self.read_float_field(diagnostic_payload, "balance_value_before"),
                    reward_text=self.read_text_field(diagnostic_payload, "reward_text"),
                    reward_kind=self.read_text_field(diagnostic_payload, "reward_kind")
                    or "unknown",
                    sell_clicked=bool(diagnostic_payload.get("sell_clicked"))
                    if diagnostic_payload
                    else False,
                    gain_value=self.read_float_field(diagnostic_payload, "gain_value"),
                    diagnostic_json_path=str(diagnostic_path) if diagnostic_path else None,
                    source_url=self.read_text_field(diagnostic_payload, "url")
                    or self.read_text_field(balance_payload, "source_url"),
                )
            )

        return results

    def load_balances_store(self) -> dict[str, Any]:
        if not self.paths.balances_file.exists():
            return {}
        try:
            return json.loads(self.paths.balances_file.read_text(encoding="utf-8"))
        except Exception:
            self.logger.exception(
                "No se pudo leer el archivo de balances en %s",
                self.paths.balances_file,
            )
            return {}

    def read_latest_balance_payload(
        self,
        balances_store: dict[str, Any],
        site_name: str,
    ) -> dict[str, Any] | None:
        site_payload = balances_store.get(site_name)
        if not isinstance(site_payload, dict):
            return None
        latest_payload = site_payload.get("latest")
        return latest_payload if isinstance(latest_payload, dict) else None

    def find_latest_diagnostic_payload(
        self,
        directory: Path,
        pattern: str,
        started_at: datetime,
        finished_at: datetime,
    ) -> tuple[Path | None, dict[str, Any] | None]:
        if not directory.exists():
            return None, None

        tolerance_before = started_at - timedelta(seconds=20)
        tolerance_after = finished_at + timedelta(seconds=20)
        matching: list[tuple[datetime, Path, dict[str, Any]]] = []

        for file_path in directory.glob(pattern):
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            captured_at_raw = payload.get("captured_at")
            if not captured_at_raw:
                continue
            try:
                captured_at = datetime.fromisoformat(str(captured_at_raw))
            except ValueError:
                continue
            if tolerance_before <= captured_at <= tolerance_after:
                matching.append((captured_at, file_path, payload))

        if not matching:
            return None, None

        matching.sort(key=lambda item: item[0], reverse=True)
        _, file_path, payload = matching[0]
        return file_path, payload

    def calculate_total_balance(
        self,
        site_results: list[SiteExecutionRecord],
    ) -> float | None:
        values = [
            site_result.balance_value
            for site_result in site_results
            if site_result.balance_value is not None
        ]
        if not values:
            return None
        return round(sum(values), 2)

    def read_text_field(
        self,
        payload: dict[str, Any] | None,
        key: str,
    ) -> str | None:
        if not payload:
            return None
        value = payload.get(key)
        if value is None:
            return None
        compact = " ".join(str(value).split()).strip()
        return compact or None

    def read_float_field(
        self,
        payload: dict[str, Any] | None,
        key: str,
    ) -> float | None:
        if not payload:
            return None
        value = payload.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
