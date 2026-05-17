from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from interaction import ask_text
from playwright.sync_api import sync_playwright
from services import SteamPresenceService
from sites.bloodycase import BloodyCaseSite
from sites.cs2free import CS2FreeSite
from sites.csgocases import DEFAULT_URL as CSGOCASES_DEFAULT_URL
from sites.csgocases import CSGOCasesSite
from sites.dropland import DroplandSite
from sites.g4skins import G4SkinsSite
from sites.keydrop import KeyDropSite, load_session as load_storage_session, save_session
from sites.steam import SteamAvatarManager
from sites.steam_playtime import SteamPlaytimeMonitor, format_hours_and_minutes

from .history import HistoryStore
from .models import ExecutionSummary, SiteExecutionRecord
from .runtime import RuntimePaths
from .settings import SITE_ORDER


class RunCancelled(Exception):
    pass


class DailyCasesRunner:
    def __init__(
        self,
        paths: RuntimePaths,
        logger: logging.Logger,
        history_store: HistoryStore | None = None,
        progress_callback: Callable[[list[dict[str, str]]], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        steam_presence_service: SteamPresenceService | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.paths = paths
        self.logger = logger
        self.history_store = history_store
        self.progress_callback = progress_callback
        self.cancel_requested = cancel_requested
        self.steam_presence_service = steam_presence_service
        self.settings = settings or {}

    def run(self) -> ExecutionSummary:
        started_at = datetime.now().astimezone()
        playtime_monitor = SteamPlaytimeMonitor(
            session_file=self.paths.steam_session_file,
            workspace_dir=self.paths.data_dir,
            data_file=self.paths.steam_playtime_file,
            logger=logging.getLogger("daily_cases_bot.steam"),
        )
        steam_presence = self.steam_presence_service or SteamPresenceService(
            script_path=self.paths.steam_presence_script,
            logger=logging.getLogger("daily_cases_bot.steam_presence"),
        )
        presence_started_by_runner = False
        enabled_sites = self.get_enabled_sites()

        recent_hours: float | None = None
        keydrop_result = "not_started"
        csgocases_result = "not_started"
        bloodycase_result = "not_started"
        cs2free_result = "not_started"
        g4skins_result = "not_started"
        dropland_result = "not_started"
        run_status = "completed"
        progress_rows = self.build_initial_progress_rows(enabled_sites)
        self.emit_progress(progress_rows)

        self.logger.info("Comprobando requisito previo de horas recientes en Steam.")
        self.logger.info("Inicializando bot para KeyDrop.")

        try:
            self.raise_if_cancelled(progress_rows)
            recent_hours = playtime_monitor.check_recent_hours_once()
            self.raise_if_cancelled(progress_rows)
            self.capture_initial_steam_profile_snapshot()
            self.raise_if_cancelled(progress_rows)
            if recent_hours < playtime_monitor.minimum_hours:
                self.logger.warning(
                    "Counter-Strike 2 no cumple el requisito: %s / %s en las ultimas 2 semanas. "
                    "Cierro el programa sin ejecutar KeyDrop.",
                    format_hours_and_minutes(recent_hours),
                    format_hours_and_minutes(playtime_monitor.minimum_hours),
                )
                run_status = "blocked_by_steam_hours"
            else:
                if self.use_presence_during_run():
                    if not steam_presence.is_running():
                        steam_presence.start()
                        presence_started_by_runner = True
                    if not steam_presence.wait_until_ready():
                        self.logger.warning(
                            "Steam Presence no quedo listo. Revisa el login/2FA y vuelve a ejecutar."
                        )
                        run_status = "steam_presence_not_ready"
                    else:
                        keydrop_result = self.run_site(
                            progress_rows,
                            "keydrop",
                            self.build_keydrop_site,
                        )
                        self.logger.info("KeyDrop finalizo con estado: %s", keydrop_result)
                        self.logger.info("Inicializando bot para CSGOCases.")
                        csgocases_result = self.run_site(
                            progress_rows,
                            "csgocases",
                            self.build_csgocases_site,
                        )
                        self.logger.info("Inicializando bot para BloodyCase.")
                        bloodycase_result = self.run_site(
                            progress_rows,
                            "bloodycase",
                            self.build_bloodycase_site,
                        )
                        self.logger.info("Inicializando bot para CS2.free.")
                        cs2free_result = self.run_site(
                            progress_rows,
                            "cs2free",
                            self.build_cs2free_site,
                        )
                        self.logger.info("Inicializando bot para G4Skins.")
                        g4skins_result = self.run_site(
                            progress_rows,
                            "g4skins",
                            self.build_g4skins_site,
                        )
                        self.logger.info("Inicializando bot para Dropland.")
                        dropland_result = self.run_site(
                            progress_rows,
                            "dropland",
                            self.build_dropland_site,
                        )
                else:
                    self.logger.info(
                        "Configuracion activa: se omite Presencia en Steam en esta ejecucion."
                    )
                    keydrop_result = self.run_site(
                        progress_rows,
                        "keydrop",
                        self.build_keydrop_site,
                    )
                    self.logger.info("KeyDrop finalizo con estado: %s", keydrop_result)
                    self.logger.info("Inicializando bot para CSGOCases.")
                    csgocases_result = self.run_site(
                        progress_rows,
                        "csgocases",
                        self.build_csgocases_site,
                    )
                    self.logger.info("Inicializando bot para BloodyCase.")
                    bloodycase_result = self.run_site(
                        progress_rows,
                        "bloodycase",
                        self.build_bloodycase_site,
                    )
                    self.logger.info("Inicializando bot para CS2.free.")
                    cs2free_result = self.run_site(
                        progress_rows,
                        "cs2free",
                        self.build_cs2free_site,
                    )
                    self.logger.info("Inicializando bot para G4Skins.")
                    g4skins_result = self.run_site(
                        progress_rows,
                        "g4skins",
                        self.build_g4skins_site,
                    )
                    self.logger.info("Inicializando bot para Dropland.")
                    dropland_result = self.run_site(
                        progress_rows,
                        "dropland",
                        self.build_dropland_site,
                    )
        except RunCancelled:
            run_status = "cancelled"
            self.mark_unfinished_sites_aborted(progress_rows)
            keydrop_result = self.read_site_result(progress_rows, "keydrop", keydrop_result)
            csgocases_result = self.read_site_result(
                progress_rows,
                "csgocases",
                csgocases_result,
            )
            bloodycase_result = self.read_site_result(
                progress_rows,
                "bloodycase",
                bloodycase_result,
            )
            cs2free_result = self.read_site_result(progress_rows, "cs2free", cs2free_result)
            g4skins_result = self.read_site_result(progress_rows, "g4skins", g4skins_result)
            dropland_result = self.read_site_result(progress_rows, "dropland", dropland_result)
            self.logger.info(
                "Ejecucion cancelada desde la interfaz. Se detuvo el flujo tras finalizar la web actual."
            )
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
                if presence_started_by_runner:
                    steam_presence.stop()
            except KeyboardInterrupt:
                self.logger.info(
                    "Cierre interrumpido por el usuario mientras se detenia Steam Presence."
                )
            self.emit_progress(progress_rows)
            self.logger.info(
                "Resumen final | Steam: %s | KeyDrop: %s | CSGOCases: %s | BloodyCase: %s | CS2.free: %s | G4Skins: %s | Dropland: %s",
                format_hours_and_minutes(recent_hours)
                if recent_hours is not None
                else "sin comprobar",
                keydrop_result,
                csgocases_result,
                bloodycase_result,
                cs2free_result,
                g4skins_result,
                dropland_result,
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
                "g4skins": g4skins_result,
                "dropland": dropland_result,
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

    def run_site(
        self,
        progress_rows: list[dict[str, str]],
        site_name: str,
        builder: Callable[[], Any],
    ) -> str:
        if not self.is_site_enabled(site_name):
            self.mark_site_completed(progress_rows, site_name, "disabled")
            return "disabled"
        self.raise_if_cancelled(progress_rows)
        self.mark_site_in_progress(progress_rows, site_name)
        result = str(builder().run())
        if self.is_cancel_requested():
            self.mark_site_completed(progress_rows, site_name, "aborted")
            raise RunCancelled()
        self.mark_site_completed(progress_rows, site_name, result)
        self.raise_if_cancelled(progress_rows)
        return result

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

    def build_initial_progress_rows(
        self,
        enabled_sites: list[str] | None = None,
    ) -> list[dict[str, str]]:
        active_sites = enabled_sites if enabled_sites is not None else self.get_enabled_sites()
        rows: list[dict[str, str]] = []
        for site_name in SITE_ORDER:
            if site_name in active_sites:
                rows.append({"site_name": site_name, "phase": "Pendiente", "result": "-"})
            else:
                rows.append({"site_name": site_name, "phase": "Hecho", "result": "disabled"})
        return rows

    def get_enabled_sites(self) -> list[str]:
        flow_settings = self.settings.get("flow")
        if not isinstance(flow_settings, dict):
            return list(SITE_ORDER)
        enabled_sites = flow_settings.get("enabled_sites")
        if not isinstance(enabled_sites, list):
            return list(SITE_ORDER)
        normalized = [str(site).strip().lower() for site in enabled_sites]
        selected = [site for site in SITE_ORDER if site in normalized]
        return selected or list(SITE_ORDER)

    def is_site_enabled(self, site_name: str) -> bool:
        return site_name.strip().lower() in self.get_enabled_sites()

    def use_presence_during_run(self) -> bool:
        steam_settings = self.settings.get("steam")
        if not isinstance(steam_settings, dict):
            return True
        return bool(steam_settings.get("use_presence_during_run", True))

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

    def is_cancel_requested(self) -> bool:
        if self.cancel_requested is None:
            return False
        try:
            return bool(self.cancel_requested())
        except Exception:
            self.logger.exception("Fallo al consultar el estado de cancelacion.")
            return False

    def raise_if_cancelled(self, progress_rows: list[dict[str, str]]) -> None:
        if not self.is_cancel_requested():
            return
        self.mark_unfinished_sites_aborted(progress_rows)
        raise RunCancelled()

    def mark_unfinished_sites_aborted(
        self,
        progress_rows: list[dict[str, str]],
    ) -> None:
        changed = False
        for row in progress_rows:
            if row.get("phase") == "Hecho":
                continue
            row["phase"] = "Hecho"
            row["result"] = "aborted"
            changed = True
        if changed:
            self.emit_progress(progress_rows)

    def read_site_result(
        self,
        progress_rows: list[dict[str, str]],
        site_name: str,
        fallback: str,
    ) -> str:
        for row in progress_rows:
            if row.get("site_name") != site_name:
                continue
            result = str(row.get("result") or fallback)
            if result and result != "-":
                return result
            return fallback
        return fallback

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

    def build_g4skins_site(self) -> G4SkinsSite:
        return G4SkinsSite(
            session_file=self.paths.g4skins_session_file,
            steam_session_file=self.paths.steam_session_file,
            workspace_dir=self.paths.data_dir,
            balances_file=self.paths.balances_file,
            logger=logging.getLogger("daily_cases_bot.g4skins"),
        )

    def build_dropland_site(self) -> DroplandSite:
        return DroplandSite(
            session_file=self.paths.dropland_session_file,
            steam_session_file=self.paths.steam_session_file,
            workspace_dir=self.paths.data_dir,
            balances_file=self.paths.balances_file,
            logger=logging.getLogger("daily_cases_bot.dropland"),
        )

    def prepare_site_session(self, site_name: str) -> str:
        normalized = site_name.strip().lower()
        if normalized == "steam":
            return self.prepare_steam_session()
        if normalized == "keydrop":
            return self.prepare_keydrop_session()
        if normalized == "csgocases":
            return self.prepare_csgocases_session()
        if normalized == "bloodycase":
            return self.prepare_bloodycase_session()
        if normalized == "cs2free":
            return self.prepare_cs2free_session()
        if normalized == "g4skins":
            return self.prepare_g4skins_session()
        if normalized == "dropland":
            return self.prepare_dropland_session()
        raise ValueError(f"Sitio no soportado para preparacion: {site_name}")

    def prepare_all_sessions(self) -> list[str]:
        messages: list[str] = []
        for site_name in ("steam", "keydrop", "csgocases", "bloodycase", "cs2free", "g4skins", "dropland"):
            try:
                messages.append(self.prepare_site_session(site_name))
            except Exception as exc:
                message = f"{site_name}: fallo durante la preparacion ({exc})"
                self.logger.exception(message)
                messages.append(message)
        return messages

    def prepare_steam_session(self) -> str:
        self.logger.info("Preparando sesion de Steam desde la interfaz.")
        steam_manager = SteamAvatarManager(
            session_file=self.paths.steam_session_file,
            workspace_dir=self.paths.data_dir,
            logger=logging.getLogger("daily_cases_bot.steam"),
        )
        try:
            steam_manager.start()
            steam_manager.backup_current_avatar()
            steam_manager.backup_current_profile_name()
            self.logger.info("Sesion de Steam preparada correctamente.")
            return "Steam preparada correctamente."
        finally:
            steam_manager.close()

    def prepare_keydrop_session(self) -> str:
        self.logger.info("Preparando sesion de KeyDrop desde la interfaz.")
        site = self.build_keydrop_site()
        with sync_playwright() as playwright:
            site.playwright = playwright
            site._open_browser(playwright)
            try:
                site.open_home_page()
                site.dismiss_cookie_banner()
                site.ensure_authenticated()
                site.dismiss_cookie_banner()
                self.logger.info("Sesion de KeyDrop preparada correctamente.")
                return "KeyDrop preparada correctamente."
            finally:
                site.close()
                site.playwright = None

    def prepare_bloodycase_session(self) -> str:
        self.logger.info("Preparando sesion de BloodyCase desde la interfaz.")
        site = self.build_bloodycase_site()
        with sync_playwright() as playwright:
            site.playwright = playwright
            site._open_browser(playwright)
            try:
                site.open_home_page()
                site.dismiss_cookie_banner()
                site.ensure_authenticated()
                site.dismiss_cookie_banner()
                self.logger.info("Sesion de BloodyCase preparada correctamente.")
                return "BloodyCase preparada correctamente."
            finally:
                site.close()
                site.playwright = None

    def prepare_cs2free_session(self) -> str:
        self.logger.info("Preparando sesion de CS2.free desde la interfaz.")
        site = self.build_cs2free_site()
        with sync_playwright() as playwright:
            site.playwright = playwright
            site._open_browser(playwright)
            try:
                site.open_home_page()
                site.ensure_authenticated()
                self.logger.info("Sesion de CS2.free preparada correctamente.")
                return "CS2.free preparada correctamente."
            finally:
                site.close()
                site.playwright = None

    def prepare_g4skins_session(self) -> str:
        self.logger.info("Preparando sesion de G4Skins desde la interfaz.")
        site = self.build_g4skins_site()
        with sync_playwright() as playwright:
            site.playwright = playwright
            site._open_browser(playwright)
            try:
                site.open_home_page()
                site.dismiss_cookie_banner()
                site.ensure_authenticated()
                site.dismiss_cookie_banner()
                self.logger.info("Sesion de G4Skins preparada correctamente.")
                return "G4Skins preparada correctamente."
            finally:
                site.close()
                site.playwright = None

    def prepare_dropland_session(self) -> str:
        self.logger.info("Preparando sesion de Dropland desde la interfaz.")
        site = self.build_dropland_site()
        with sync_playwright() as playwright:
            site.playwright = playwright
            site._open_browser(playwright)
            try:
                site.open_home_page()
                site.dismiss_cookie_banner()
                site.ensure_authenticated()
                site.dismiss_cookie_banner()
                self.logger.info("Sesion de Dropland preparada correctamente.")
                return "Dropland preparada correctamente."
            finally:
                site.close()
                site.playwright = None

    def prepare_csgocases_session(self) -> str:
        self.logger.info("Preparando sesion de CSGOCases desde la interfaz.")
        site = self.build_csgocases_site()
        session_data = load_storage_session(site.session_file, site.logger)
        with sync_playwright() as playwright:
            browser, context, page = site.open_balance_check_browser(playwright, session_data)
            try:
                page.goto(CSGOCASES_DEFAULT_URL, wait_until="domcontentloaded", timeout=25_000)
                site.wait_for_light_page_ready(page)
                if site.find_balance_text(page):
                    self.logger.info("Sesion reutilizada automaticamente en CSGOCases.")
                    save_session(context, site.session_file, site.logger)
                    return "CSGOCases preparada correctamente."

                self.logger.warning(
                    "No hay sesion valida de CSGOCases. Se requiere login manual."
                )
                while True:
                    answer = ask_text(
                        "Haz login manual en CSGOCases en la ventana de Chromium y luego pulsa Enter aqui. "
                        "Escribe 'q' para cancelar: ",
                        title="Login manual en CSGOCases",
                    ).strip().lower()
                    if answer in {"q", "quit", "exit"}:
                        raise RuntimeError("Login manual de CSGOCases cancelado por el usuario.")
                    page.goto(CSGOCASES_DEFAULT_URL, wait_until="domcontentloaded", timeout=25_000)
                    site.wait_for_light_page_ready(page)
                    if site.find_balance_text(page):
                        save_session(context, site.session_file, site.logger)
                        self.logger.info("Sesion de CSGOCases preparada correctamente.")
                        return "CSGOCases preparada correctamente."
                    retry = ask_text(
                        "No se pudo confirmar el login en CSGOCases. Enter para revisar otra vez o 'q' para salir: ",
                        title="Reintento de login en CSGOCases",
                    ).strip().lower()
                    if retry in {"q", "quit", "exit"}:
                        raise RuntimeError("Login manual de CSGOCases cancelado por el usuario.")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    def collect_site_results(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        site_statuses: dict[str, str],
    ) -> list[SiteExecutionRecord]:
        diagnostics_config = {
            "keydrop": ("keydrop_daily_case", "keydrop_*.json"),
            "csgocases": ("csgocases_daily_free", "daily_free_*.json"),
            "bloodycase": ("bloodycase_daily_free", "bloodycase_*.json"),
            "cs2free": ("cs2free_daily", "cs2free_*.json"),
            "g4skins": ("g4skins_daily_case", "g4skins_*.json"),
            "dropland": ("dropland_daily_free", "dropland_*.json"),
        }
        balances_store = self.load_balances_store()
        results: list[SiteExecutionRecord] = []

        for site_name in ("keydrop", "csgocases", "bloodycase", "cs2free", "g4skins", "dropland"):
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
