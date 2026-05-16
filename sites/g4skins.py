from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from interaction import ask_text
from .keydrop import load_session, save_balance_snapshot, save_session
from .steam import SteamAvatarManager

DEFAULT_URL = "https://g4skins.com/daily-case"
OPEN_URL = "https://g4skins.com/daily-case/open"
SITE_NAME = "g4skins"
STEAM_REQUIRED_NAME = "skins.army"
BALANCE_SELECTOR = (
    "#layout_content nav div div div.user div.account div.info div.wallet div.balance"
)
DAILY_ENTRY_SELECTOR = (
    "#layout_content main div div div div.daily-levels div div.levels-level.__active div.level-info a"
)
OPEN_ACTION_SELECTOR = (
    "#layout_content main div div div.case-top div.top-options a"
)
LOGIN_PATTERN = re.compile(r"(iniciar sesi[oó]n|login|sign in|steam)", re.IGNORECASE)
MAIN_ACTION_PATTERN = re.compile(
    r"(open case|abrir caja|meet the requirements|cumple los requisitos)",
    re.IGNORECASE,
)
OPEN_PATTERN = re.compile(r"(open case|abrir caja)", re.IGNORECASE)
OPENED_PATTERN = re.compile(r"(open cases|abrir cajas)", re.IGNORECASE)
REQUIREMENTS_PATTERN = re.compile(
    r"(meet the requirements|cumple los requisitos)",
    re.IGNORECASE,
)
REFRESH_REQUIREMENTS_PATTERN = re.compile(
    r"(refresh requirements|refrescar requisitos|actualizar requisitos|verificar requisitos|check requirements)",
    re.IGNORECASE,
)
REQUIREMENTS_FEEDBACK_PATTERN = re.compile(
    r"(requisit|requirement|required|steam name|profile name|nickname|nick de steam|cumple los requisitos)",
    re.IGNORECASE,
)
COOLDOWN_PATTERN = re.compile(
    r"(available in|cooldown|wait|\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b|\b\d+\s*h\b|\b\d+\s*min\b)",
    re.IGNORECASE,
)
SELL_PATTERN = re.compile(r"(sell|vender)", re.IGNORECASE)
EMPTY_ITEMS_PATTERN = re.compile(
    r"(sin art[ií]culos|total de art[ií]culos:\s*0[.,]00)",
    re.IGNORECASE,
)
EXP_REWARD_PATTERN = re.compile(
    r"(\b\d+\s*exp\b|nivel\s*\d+\s*\+\s*\d+\s*exp|\bexp\b)",
    re.IGNORECASE,
)
DROP_SECTION_PATTERN = re.compile(r"(tu ca[ií]da|your drop)", re.IGNORECASE)
WEAPON_REWARD_PATTERN = re.compile(
    r"\b("
    r"ak-47|m4a1-s|m4a4|awp|usp-s|glock-18|deagle|desert eagle|p250|famas|galil|galil ar|"
    r"mp9|mp7|mp5-sd|ump-45|p90|aug|sg 553|xm1014|mag-7|nova|sawed-off|mac-10|"
    r"five-seven|cz75|tec-9|dual berettas|ssg 08|negev|m249|knife|karambit|bayonet|"
    r"butterfly|falchion|shadow daggers|huntsman|ursus|talon|stiletto|navaja|"
    r"gloves|guantes"
    r")\b",
    re.IGNORECASE,
)
GENERIC_UI_TEXT_PATTERN = re.compile(
    r"(g4skins|daily case|open case|meet the requirements|sell|upgrade|battle|"
    r"contract|daily|case|balance|wallet|requirement|steam)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class G4SkinsSite:
    session_file: Path
    steam_session_file: Path
    workspace_dir: Path
    balances_file: Path
    logger: logging.Logger
    url: str = DEFAULT_URL
    open_url: str = OPEN_URL
    headless: bool = False
    slow_mo_ms: int = 90
    browser: Browser | None = field(default=None, init=False)
    context: BrowserContext | None = field(default=None, init=False)
    page: Page | None = field(default=None, init=False)
    playwright: Playwright | None = field(default=None, init=False)
    diagnostics_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.diagnostics_dir = self.workspace_dir / "g4skins_daily_case"
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> str:
        with sync_playwright() as playwright:
            self.playwright = playwright
            self._open_browser(playwright)

            try:
                while True:
                    steam_manager: SteamAvatarManager | None = None
                    try:
                        steam_requirement_applied = False
                        self.open_home_page()
                        self.dismiss_cookie_banner()
                        self.ensure_authenticated()
                        self.dismiss_cookie_banner()
                        balance_text = self.read_balance_text()
                        balance_value = self.parse_balance_value(balance_text)
                        self.persist_balance(balance_text, balance_value)

                        self.open_case_page()
                        button_text = self.inspect_open_action_text()
                        if not button_text:
                            self.capture_diagnostics(
                                status="button_not_found",
                                balance_text_before=balance_text,
                                balance_value_before=balance_value,
                                button_text=None,
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons=[],
                                sell_button_text=None,
                            )
                            return "cooldown"

                        if REQUIREMENTS_PATTERN.search(button_text):
                            steam_manager = self.apply_steam_requirement()
                            steam_requirement_applied = True
                            button_text = (
                                self.refresh_requirements_after_steam_change()
                                or self.inspect_open_action_text()
                                or button_text
                            )

                        if REQUIREMENTS_PATTERN.search(button_text):
                            self.capture_diagnostics(
                                status="account_setup_required",
                                balance_text_before=balance_text,
                                balance_value_before=balance_value,
                                button_text=button_text,
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons=[],
                                sell_button_text=None,
                            )
                            self.logger.warning(
                                "G4Skins sigue mostrando requisitos pendientes tras cambiar el nick de Steam."
                            )
                            return "account_setup_required"

                        if COOLDOWN_PATTERN.search(button_text):
                            self.capture_diagnostics(
                                status="cooldown",
                                balance_text_before=balance_text,
                                balance_value_before=balance_value,
                                button_text=button_text,
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons=[],
                                sell_button_text=None,
                            )
                            self.logger.info(
                                "G4Skins muestra la daily case en cooldown/no disponible. Texto detectado: %s",
                                button_text,
                            )
                            return "cooldown"

                        if not OPEN_PATTERN.search(button_text):
                            self.capture_diagnostics(
                                status="not_claimable",
                                balance_text_before=balance_text,
                                balance_value_before=balance_value,
                                button_text=button_text,
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons=[],
                                sell_button_text=None,
                            )
                            self.logger.info(
                                "G4Skins no esta en estado OPEN CASE. Texto detectado: %s",
                                button_text,
                            )
                            return "not_claimable"

                        self.click_open_action()
                        post_open_status = self.handle_post_open_state(
                            balance_text_before=balance_text,
                            balance_value_before=balance_value,
                            button_text=button_text,
                        )
                        if post_open_status == "open_not_confirmed":
                            self.logger.info(
                                "La apertura de G4Skins no se confirmo tras el primer click. Se reintenta pulsando ABRIR CAJA."
                            )
                            self.dismiss_cookie_banner()
                            retry_button_text = self.inspect_open_action_text() or button_text
                            if REQUIREMENTS_PATTERN.search(retry_button_text):
                                post_open_status = "requirements_pending"
                            elif OPEN_PATTERN.search(retry_button_text):
                                self.click_open_action()
                                post_open_status = self.handle_post_open_state(
                                    balance_text_before=balance_text,
                                    balance_value_before=balance_value,
                                    button_text=retry_button_text,
                                )
                        if post_open_status == "requirements_pending":
                            if not steam_requirement_applied:
                                self.logger.info(
                                    "G4Skins rechazo la apertura pese a mostrar ABRIR CAJA. Se aplican requisitos de Steam y se reintenta."
                                )
                                steam_manager = self.apply_steam_requirement()
                                steam_requirement_applied = True
                                button_text = (
                                    self.refresh_requirements_after_steam_change()
                                    or self.inspect_open_action_text()
                                    or button_text
                                )
                                if REQUIREMENTS_PATTERN.search(button_text):
                                    self.logger.warning(
                                        "G4Skins sigue mostrando requisitos pendientes tras el reintento posterior al toast o rechazo de apertura."
                                    )
                                    return "account_setup_required"
                                if not OPEN_PATTERN.search(button_text):
                                    self.logger.warning(
                                        "G4Skins no quedo en estado ABRIR CAJA tras reaplicar requisitos. Texto detectado: %s",
                                        button_text,
                                    )
                                    return "account_setup_required"
                                self.click_open_action()
                                post_open_status = self.handle_post_open_state(
                                    balance_text_before=balance_text,
                                    balance_value_before=balance_value,
                                    button_text=button_text,
                                )
                                if post_open_status == "open_not_confirmed":
                                    self.logger.info(
                                        "Tras aplicar requisitos, G4Skins no confirmo la apertura al primer click. Se reintenta una vez mas."
                                    )
                                    retry_button_text = self.inspect_open_action_text() or button_text
                                    if OPEN_PATTERN.search(retry_button_text):
                                        self.click_open_action()
                                        post_open_status = self.handle_post_open_state(
                                            balance_text_before=balance_text,
                                            balance_value_before=balance_value,
                                            button_text=retry_button_text,
                                        )
                            if post_open_status == "requirements_pending":
                                self.logger.warning(
                                    "G4Skins sigue rechazando la apertura por requisitos tras el reintento."
                                )
                                return "account_setup_required"
                        if post_open_status == "open_not_confirmed":
                            self.logger.warning(
                                "G4Skins no confirmo la apertura de la caja tras varios intentos pese a mostrar ABRIR CAJA."
                            )
                            return "aborted"
                        self.logger.info(
                            "Flujo de G4Skins finalizado. Saldo detectado: %s | Estado postapertura: %s",
                            balance_text,
                            post_open_status,
                        )
                        return post_open_status
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        self.logger.exception(
                            "Error controlado en G4Skins. El navegador seguira abierto."
                        )
                        if self.context is not None:
                            save_session(self.context, self.session_file, self.logger)
                        if not self.prompt_retry():
                            return "aborted"
                    finally:
                        self.cleanup_steam_requirement(steam_manager)
            finally:
                self.close()
                self.playwright = None

    def _open_browser(self, playwright: Playwright) -> None:
        session_data = load_session(self.session_file, self.logger)
        self.browser = playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 920},
            "locale": "es-ES",
            "timezone_id": "Europe/Madrid",
            "color_scheme": "light",
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "extra_http_headers": {
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
        }
        if session_data:
            context_kwargs["storage_state"] = session_data

        self.context = self.browser.new_context(**context_kwargs)
        self.context.set_default_timeout(15_000)
        self.context.set_default_navigation_timeout(30_000)
        self.context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['es-ES', 'es', 'en-US', 'en']
            });
            """
        )
        self.page = self.context.new_page()
        self.page.bring_to_front()
        self.logger.info("Chromium visible iniciado para G4Skins.")

    def open_home_page(self) -> None:
        assert self.page is not None
        self.logger.info("Abriendo %s", self.url)
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_for_page_ready()
        self.human_delay(1.2, 2.4)

    def wait_for_page_ready(self) -> None:
        assert self.page is not None
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de G4Skins no entro en domcontentloaded. Se continua con timeout controlado."
            )
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de G4Skins no entro en networkidle. Se continua con timeout controlado."
            )
        self.page.locator("body").wait_for(state="visible", timeout=10_000)

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        if self.is_logged_in():
            self.logger.info("Sesion reutilizada automaticamente en G4Skins.")
            return

        self.logger.warning("No hay sesion valida de G4Skins. Se requiere login manual.")
        while True:
            ask_text(
                "Haz login manual en G4Skins en la ventana de Chromium y luego pulsa Enter aqui. ",
                title="Login manual en G4Skins",
            )
            self.human_delay(1.5, 2.8)
            self.page.goto(self.url, wait_until="domcontentloaded")
            self.wait_for_page_ready()

            if self.is_logged_in():
                save_session(self.context, self.session_file, self.logger)
                self.logger.info("Login manual detectado en G4Skins y sesion guardada.")
                return

            answer = ask_text(
                "No se pudo confirmar el login en G4Skins. Enter para revisar otra vez o 'q' para salir: ",
                title="Reintento de login en G4Skins",
            ).strip().lower()
            if answer in {"q", "quit", "exit"}:
                raise RuntimeError("Login manual de G4Skins cancelado por el usuario.")

    def is_logged_in(self) -> bool:
        assert self.page is not None

        auth_locators = [
            self.page.locator(BALANCE_SELECTOR).first,
            self.page.locator(DAILY_ENTRY_SELECTOR).first,
            self.page.locator(OPEN_ACTION_SELECTOR).first,
        ]
        if self.first_visible(auth_locators, timeout_ms=4_000) is not None:
            return True

        guest_locators = [
            self.page.get_by_role("button", name=LOGIN_PATTERN).first,
            self.page.get_by_role("link", name=LOGIN_PATTERN).first,
            self.page.locator("a[href*='login']").first,
            self.page.locator("a[href*='steam']").first,
        ]
        return self.first_visible(guest_locators, timeout_ms=1_500) is None

    def dismiss_cookie_banner(self) -> None:
        assert self.page is not None
        cookie_locators = [
            self.page.get_by_role(
                "button",
                name=re.compile(r"(aceptar|accept|allow all)", re.IGNORECASE),
            ).first,
            self.page.locator("button:has-text('Aceptar')").first,
            self.page.locator("button:has-text('Accept')").first,
        ]
        banner_button = self.first_visible(cookie_locators, timeout_ms=2_500)
        if banner_button is None:
            return
        self.safe_click(banner_button, "banner de cookies de G4Skins", allow_fail=True)

    def read_balance_text(self) -> str:
        assert self.page is not None
        locator = self.page.locator(BALANCE_SELECTOR).first
        locator.wait_for(state="visible", timeout=12_000)
        self.human_delay(0.5, 1.0)
        balance_text = self.compact_text(locator.text_content(timeout=5_000) or "")
        if not balance_text:
            raise RuntimeError("No se pudo leer el saldo de G4Skins.")
        self.logger.info("Saldo extraido desde la cabecera de G4Skins: %s", balance_text)
        return balance_text

    def parse_balance_value(self, balance_text: str) -> float | None:
        cleaned = re.sub(r"[^\d,.\-]", "", balance_text).strip()
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            self.logger.warning(
                "No se pudo convertir el saldo de G4Skins '%s' a numero.",
                balance_text,
            )
            return None

    def persist_balance(self, balance_text: str, balance_value: float | None) -> None:
        saved = save_balance_snapshot(
            balances_file=self.balances_file,
            site_name=SITE_NAME,
            balance_text=balance_text,
            balance_value=balance_value,
            source_url=self.page.url if self.page is not None else self.url,
            logger=self.logger,
        )
        if not saved:
            raise RuntimeError("No se pudo persistir el saldo de G4Skins.")

    def open_case_page(self) -> None:
        assert self.page is not None
        entry = self.page.locator(DAILY_ENTRY_SELECTOR).first
        try:
            entry.wait_for(state="visible", timeout=8_000)
            entry_text = self.compact_text(entry.inner_text(timeout=3_000))
            self.logger.info("Boton de entrada a la caja diaria de G4Skins: %s", entry_text)
            self.safe_click(entry, "entrada OPEN CASE de G4Skins")
            self.wait_for_page_ready()
        except Exception:
            self.logger.info(
                "No se pudo usar el boton inicial de G4Skins. Se abre directamente %s",
                self.open_url,
            )
            self.page.goto(self.open_url, wait_until="domcontentloaded")
            self.wait_for_page_ready()
        self.human_delay(1.0, 2.0)

    def inspect_open_action_text(self) -> str | None:
        button = self.find_open_action_locator(timeout_ms=12_000)
        if button is None:
            self.logger.warning("No se encontro el boton principal de G4Skins.")
            return None
        button_text = self.compact_text(button.text_content(timeout=5_000) or "")
        self.logger.info("Boton principal de G4Skins detectado con texto: %s", button_text)
        return button_text or None

    def click_open_action(self) -> None:
        button = self.find_open_action_locator(timeout_ms=8_000)
        if button is None:
            raise RuntimeError("No se encontro el boton principal de G4Skins para abrir la caja.")
        self.safe_click(button, "boton OPEN CASE de G4Skins")

    def find_open_action_locator(self, timeout_ms: int = 3_500) -> Locator | None:
        assert self.page is not None
        action_locators = [
            self.page.locator(OPEN_ACTION_SELECTOR).first,
            self.page.get_by_role("button", name=MAIN_ACTION_PATTERN).first,
            self.page.get_by_role("link", name=MAIN_ACTION_PATTERN).first,
            self.page.locator("button").filter(has_text=MAIN_ACTION_PATTERN).first,
            self.page.locator("a").filter(has_text=MAIN_ACTION_PATTERN).first,
        ]
        return self.first_visible(action_locators, timeout_ms=timeout_ms)

    def apply_steam_requirement(self) -> SteamAvatarManager:
        if self.playwright is None:
            raise RuntimeError("No hay instancia activa de Playwright en G4Skins.")
        steam_manager = SteamAvatarManager(
            session_file=self.steam_session_file,
            workspace_dir=self.workspace_dir,
            logger=logging.getLogger("daily_cases_bot.steam"),
        )
        try:
            steam_manager.start(playwright=self.playwright)
            temporary_name = steam_manager.backup_and_apply_profile_name_prefix_suffix(
                STEAM_REQUIRED_NAME,
                prefix_length=3,
            )
            self.logger.info(
                "Nick temporal corto de Steam aplicado para G4Skins con sufijo %s. Resultado: %s",
                STEAM_REQUIRED_NAME,
                temporary_name,
            )
            self.human_delay(2.0, 4.0)
            return steam_manager
        except Exception:
            steam_manager.close()
            raise

    def cleanup_steam_requirement(
        self,
        steam_manager: SteamAvatarManager | None,
    ) -> None:
        if steam_manager is None:
            return
        try:
            restored = steam_manager.restore_previous_profile_name()
            if not restored:
                self.logger.warning(
                    "No se pudo restaurar automaticamente el nick original de Steam tras G4Skins."
                )
        except Exception:
            self.logger.exception(
                "Fallo durante la restauracion del nick de Steam tras G4Skins."
            )
        finally:
            try:
                steam_manager.close()
            except Exception:
                self.logger.exception("Fallo al cerrar SteamAvatarManager tras G4Skins.")

    def reload_open_page_after_steam_change(self) -> None:
        assert self.page is not None
        self.logger.info(
            "Recargando G4Skins tras el cambio temporal del nick de Steam para refrescar requisitos."
        )
        self.human_delay(2.0, 4.0)
        self.page.goto(self.open_url, wait_until="domcontentloaded")
        self.wait_for_page_ready()
        self.human_delay(2.0, 4.0)

    def refresh_requirements_after_steam_change(self) -> str | None:
        assert self.page is not None
        self.logger.info(
            "G4Skins requiere refrescar requisitos tras cambiar el nick de Steam."
        )

        self.click_requirements_button_if_visible()
        refresh_clicked = self.click_refresh_requirements_button_if_visible()
        self.human_delay(2.0, 3.5)

        button_text = self.inspect_open_action_text_soft()
        if button_text and OPEN_PATTERN.search(button_text):
            self.logger.info(
                "G4Skins ya quedo listo para abrir la caja tras refrescar los requisitos. Texto detectado: %s",
                button_text,
            )
            return button_text
        if button_text and not REQUIREMENTS_PATTERN.search(button_text):
            self.logger.info(
                "G4Skins cambio el estado del boton principal tras refrescar requisitos: %s",
                button_text,
            )
            return button_text

        if refresh_clicked:
            self.logger.info(
                "G4Skins sigue sin mostrar el estado final tras refrescar requisitos. Se recarga la pagina para confirmar."
            )
        else:
            self.logger.info(
                "G4Skins no necesita un click efectivo en refrescar requisitos o el boton ya no era interactuable. Se comprueba la pagina recargando como fallback."
            )
        self.reload_open_page_after_steam_change()
        return self.inspect_open_action_text_soft()

    def click_requirements_button_if_visible(self) -> bool:
        assert self.page is not None
        requirement_locators = [
            self.page.get_by_role("button", name=REQUIREMENTS_PATTERN).first,
            self.page.get_by_role("link", name=REQUIREMENTS_PATTERN).first,
            self.page.locator("button").filter(has_text=REQUIREMENTS_PATTERN).first,
            self.page.locator("a").filter(has_text=REQUIREMENTS_PATTERN).first,
            self.page.locator(OPEN_ACTION_SELECTOR).first,
        ]
        locator = self.first_visible(requirement_locators, timeout_ms=4_000)
        if locator is None:
            self.logger.info(
                "No se encontro el boton CUMPLE LOS REQUISITOS para abrir el panel de requisitos."
            )
            return False
        self.logger.info(
            "G4Skins requiere nick temporal de Steam; se aplica %s y se abre el panel de requisitos.",
            STEAM_REQUIRED_NAME,
        )
        return self.safe_click(locator, "boton CUMPLE LOS REQUISITOS de G4Skins", allow_fail=True)

    def click_refresh_requirements_button_if_visible(self) -> bool:
        assert self.page is not None
        self.human_delay(1.2, 2.2)
        refresh_locators = [
            self.page.get_by_role("button", name=REFRESH_REQUIREMENTS_PATTERN).first,
            self.page.get_by_role("link", name=REFRESH_REQUIREMENTS_PATTERN).first,
            self.page.locator("button").filter(has_text=REFRESH_REQUIREMENTS_PATTERN).first,
            self.page.locator("a").filter(has_text=REFRESH_REQUIREMENTS_PATTERN).first,
            self.page.locator("[role='button']").filter(has_text=REFRESH_REQUIREMENTS_PATTERN).first,
        ]
        locator = self.first_visible(refresh_locators, timeout_ms=5_000)
        if locator is None:
            self.logger.info(
                "No se encontro el boton de refrescar requisitos de G4Skins."
            )
            return False
        self.logger.info("Click en el boton de refrescar requisitos de G4Skins.")
        clicked = self.safe_click(
            locator,
            "boton refrescar requisitos de G4Skins",
            allow_fail=True,
            failure_level="info",
        )
        if not clicked:
            self.logger.info(
                "El boton de refrescar requisitos de G4Skins ya no era interactuable. Puede que la web hubiera validado los requisitos automaticamente."
            )
        return clicked

    def inspect_open_action_text_soft(self) -> str | None:
        button = self.find_open_action_locator(timeout_ms=5_000)
        if button is None:
            return None
        try:
            button_text = self.compact_text(button.text_content(timeout=4_000) or "")
        except Exception:
            return None
        if button_text:
            self.logger.info(
                "Estado actual del boton principal de G4Skins tras revisar requisitos: %s",
                button_text,
            )
            return button_text
        return None

    def handle_post_open_state(
        self,
        *,
        balance_text_before: str,
        balance_value_before: float | None,
        button_text: str | None,
    ) -> str:
        assert self.page is not None
        self.logger.info(
            "Esperando el estado posterior a la apertura de la daily case de G4Skins."
        )
        notification_texts: list[str] = []
        body_text: str | None = None
        current_button_text: str | None = button_text
        opened_state_detected = False
        for _ in range(12):
            self.human_delay(0.8, 1.2)
            notification_texts = self.collect_visible_notification_texts()
            if any(REQUIREMENTS_FEEDBACK_PATTERN.search(text) for text in notification_texts):
                self.logger.warning(
                    "G4Skins mostro feedback de requisitos pendientes tras pulsar ABRIR CAJA: %s",
                    " | ".join(notification_texts),
                )
                self.capture_diagnostics(
                    status="requirements_pending",
                    balance_text_before=balance_text_before,
                    balance_value_before=balance_value_before,
                    button_text=current_button_text,
                    reward_text=None,
                    reward_kind="unknown",
                    reward_candidates=[],
                    visible_buttons=notification_texts,
                    sell_button_text=None,
                )
                return "requirements_pending"

            body_text = self.try_read_body_text()
            current_button_text = self.inspect_open_action_text_soft() or current_button_text
            if self.page_looks_opened(body_text, current_button_text):
                opened_state_detected = True
                break

        reward_candidates = self.collect_visible_text_candidates()
        visible_buttons = self.collect_visible_button_texts()
        if body_text is None:
            body_text = self.try_read_body_text()
        current_button_text = self.inspect_open_action_text_soft() or current_button_text

        if not opened_state_detected and self.page_still_blocked_for_open(body_text, current_button_text):
            self.capture_diagnostics(
                status="open_not_confirmed",
                balance_text_before=balance_text_before,
                balance_value_before=balance_value_before,
                button_text=current_button_text,
                reward_text=None,
                reward_kind="unknown",
                reward_candidates=reward_candidates,
                visible_buttons=visible_buttons,
                sell_button_text=None,
            )
            self.logger.warning(
                "G4Skins no confirmo la apertura tras pulsar ABRIR CAJA. La pagina sigue sin reflejar una recompensa."
            )
            return "open_not_confirmed"

        self.logger.info(
            "G4Skins parece haber completado la apertura. Se espera unos segundos para capturar el estado final de la recompensa."
        )
        self.human_delay(3.5, 5.5)
        body_text = self.try_read_body_text() or body_text
        current_button_text = self.inspect_open_action_text_soft() or current_button_text
        reward_candidates = self.collect_visible_text_candidates()
        visible_buttons = self.collect_visible_button_texts()

        if self.page_is_cooldown_after_open(body_text, current_button_text):
            self.capture_diagnostics(
                status="cooldown",
                balance_text_before=balance_text_before,
                balance_value_before=balance_value_before,
                button_text=current_button_text,
                reward_text=None,
                reward_kind="unknown",
                reward_candidates=reward_candidates,
                visible_buttons=visible_buttons,
                sell_button_text=None,
            )
            self.logger.info(
                "G4Skins ya muestra cooldown tras la apertura de la caja. Texto detectado: %s",
                current_button_text,
            )
            return "cooldown"

        reward_text = self.infer_reward_text(reward_candidates, body_text=body_text)
        reward_kind = self.infer_reward_kind(reward_text, body_text=body_text)
        sell_button_text = next(
            (text for text in visible_buttons if SELL_PATTERN.search(text)),
            None,
        )
        self.capture_diagnostics(
            status="opened_unsold",
            balance_text_before=balance_text_before,
            balance_value_before=balance_value_before,
            button_text=current_button_text,
            reward_text=reward_text,
            reward_kind=reward_kind,
            reward_candidates=reward_candidates,
            visible_buttons=visible_buttons,
            sell_button_text=sell_button_text,
        )
        return "opened_unsold"

    def collect_visible_text_candidates(self) -> list[dict[str, Any]]:
        assert self.page is not None
        try:
            items = self.page.evaluate(
                """
                () => {
                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style &&
                      style.visibility !== 'hidden' &&
                      style.display !== 'none' &&
                      rect.width > 0 &&
                      rect.height > 0;
                  };
                  const candidates = [];
                  for (const el of document.querySelectorAll('body *')) {
                    if (!isVisible(el)) continue;
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!text || text.length < 2 || text.length > 80) continue;
                    const rect = el.getBoundingClientRect();
                    candidates.push({
                      text,
                      x: rect.left,
                      y: rect.top,
                      width: rect.width,
                      height: rect.height,
                      tagName: el.tagName,
                    });
                  }
                  return candidates.slice(0, 500);
                }
                """
            )
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def collect_visible_button_texts(self) -> list[str]:
        assert self.page is not None
        try:
            items = self.page.evaluate(
                """
                () => {
                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style &&
                      style.visibility !== 'hidden' &&
                      style.display !== 'none' &&
                      rect.width > 0 &&
                      rect.height > 0;
                  };
                  const results = [];
                  for (const el of document.querySelectorAll('button, a')) {
                    if (!isVisible(el)) continue;
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (text) results.push(text);
                  }
                  return results;
                }
                """
            )
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [self.compact_text(str(item)) for item in items if str(item).strip()]

    def infer_reward_text(
        self,
        reward_candidates: list[dict[str, Any]],
        *,
        body_text: str | None = None,
    ) -> str | None:
        if body_text:
            exp_match = re.search(r"\b(\d+\s*EXP)\b", body_text, re.IGNORECASE)
            if exp_match:
                return self.compact_text(exp_match.group(1))

        filtered: list[dict[str, Any]] = []
        for item in reward_candidates:
            text = self.compact_text(str(item.get("text", "")))
            if not text:
                continue
            if GENERIC_UI_TEXT_PATTERN.search(text):
                continue
            filtered.append(item)
        weapon_matches = [
            item for item in filtered if WEAPON_REWARD_PATTERN.search(str(item.get("text", "")))
        ]
        if weapon_matches:
            weapon_matches.sort(key=lambda item: (float(item.get("y", 0) or 0), -float(item.get("height", 0) or 0)))
            return self.compact_text(str(weapon_matches[0].get("text", ""))) or None
        filtered.sort(key=lambda item: (float(item.get("y", 0) or 0), -float(item.get("height", 0) or 0)))
        if not filtered:
            return None
        return self.compact_text(str(filtered[0].get("text", ""))) or None

    def infer_reward_kind(
        self,
        reward_text: str | None,
        *,
        body_text: str | None = None,
    ) -> str:
        if reward_text and WEAPON_REWARD_PATTERN.search(reward_text):
            return "skin"
        if reward_text and EXP_REWARD_PATTERN.search(reward_text):
            return "exp"
        if body_text and DROP_SECTION_PATTERN.search(body_text) and EXP_REWARD_PATTERN.search(body_text):
            return "exp"
        return "unknown"

    def collect_visible_notification_texts(self) -> list[str]:
        assert self.page is not None
        notification_locators = [
            self.page.locator("[role='alert']").all(),
            self.page.locator("[aria-live='assertive']").all(),
            self.page.locator("[aria-live='polite']").all(),
            self.page.locator(".toast").all(),
            self.page.locator(".Toastify__toast").all(),
            self.page.locator("[data-sonner-toast]").all(),
        ]
        results: list[str] = []
        seen: set[str] = set()
        for locators in notification_locators:
            for locator in locators:
                try:
                    if not locator.is_visible():
                        continue
                    text = self.compact_text(locator.inner_text(timeout=1_500))
                except Exception:
                    continue
                if not text or text in seen:
                    continue
                seen.add(text)
                results.append(text)
        return results

    def try_read_body_text(self) -> str | None:
        assert self.page is not None
        try:
            body_text = self.page.locator("body").inner_text(timeout=3_000)
        except Exception:
            return None
        compacted = self.compact_text(body_text)
        return compacted or None

    def page_still_blocked_for_open(
        self,
        body_text: str | None,
        button_text: str | None,
    ) -> bool:
        if button_text and REQUIREMENTS_PATTERN.search(button_text):
            return True
        if (
            button_text
            and OPEN_PATTERN.search(button_text)
            and body_text
            and EMPTY_ITEMS_PATTERN.search(body_text)
            and not DROP_SECTION_PATTERN.search(body_text)
        ):
            return True
        return False

    def page_looks_opened(
        self,
        body_text: str | None,
        button_text: str | None,
    ) -> bool:
        if not body_text:
            return False
        if DROP_SECTION_PATTERN.search(body_text):
            return True
        if EXP_REWARD_PATTERN.search(body_text) and OPENED_PATTERN.search(button_text or ""):
            return True
        if (
            button_text
            and OPENED_PATTERN.search(button_text)
            and body_text
            and not COOLDOWN_PATTERN.search(body_text)
        ):
            return True
        if EMPTY_ITEMS_PATTERN.search(body_text):
            return False
        if button_text and REQUIREMENTS_PATTERN.search(button_text):
            return False
        return True

    def page_is_cooldown_after_open(
        self,
        body_text: str | None,
        button_text: str | None,
    ) -> bool:
        if not body_text or not button_text:
            return False
        if not OPENED_PATTERN.search(button_text):
            return False
        if DROP_SECTION_PATTERN.search(body_text):
            return False
        return COOLDOWN_PATTERN.search(body_text) is not None

    def capture_diagnostics(
        self,
        *,
        status: str,
        balance_text_before: str,
        balance_value_before: float | None,
        button_text: str | None,
        reward_text: str | None,
        reward_kind: str,
        reward_candidates: list[dict[str, Any]],
        visible_buttons: list[str],
        sell_button_text: str | None,
    ) -> None:
        assert self.page is not None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_file = self.diagnostics_dir / f"g4skins_{timestamp}.png"
        text_file = self.diagnostics_dir / f"g4skins_{timestamp}.txt"
        json_file = self.diagnostics_dir / f"g4skins_{timestamp}.json"
        try:
            self.page.screenshot(path=str(screenshot_file), full_page=True)
            body_text = self.page.locator("body").inner_text(timeout=10_000)
            text_file.write_text(body_text, encoding="utf-8")
            snapshot = {
                "captured_at": datetime.now().astimezone().isoformat(),
                "url": self.page.url,
                "status": status,
                "balance_text_before": balance_text_before,
                "balance_value_before": balance_value_before,
                "button_text": button_text,
                "reward_text": reward_text,
                "reward_kind": reward_kind,
                "reward_candidates": reward_candidates[:25],
                "visible_buttons": visible_buttons,
                "sell_button_text": sell_button_text,
            }
            json_file.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.logger.info(
                "Diagnostico de G4Skins guardado en %s, %s y %s.",
                screenshot_file,
                text_file,
                json_file,
            )
        except Exception:
            self.logger.exception("No se pudo capturar el diagnostico de G4Skins.")

    def compact_text(self, value: str) -> str:
        return " ".join(value.split()).strip()

    def safe_click(
        self,
        locator: Locator,
        description: str,
        allow_fail: bool = False,
        failure_level: str = "warning",
    ) -> bool:
        try:
            locator.wait_for(state="visible", timeout=10_000)
            locator.scroll_into_view_if_needed(timeout=5_000)
            self.human_delay(0.4, 1.1)
            try:
                locator.hover(timeout=3_000)
                self.human_delay(0.2, 0.7)
            except PlaywrightTimeoutError:
                self.logger.info(
                    "No se pudo hacer hover sobre %s en G4Skins. Se intenta click.",
                    description,
                )
            locator.click(timeout=10_000)
            self.logger.info("Click realizado en %s.", description)
            return True
        except Exception:
            if allow_fail:
                if failure_level == "info":
                    self.logger.info("No se pudo interactuar con %s.", description)
                else:
                    self.logger.warning("No se pudo interactuar con %s.", description)
                return False
            raise

    def first_visible(
        self,
        locators: list[Locator],
        timeout_ms: int = 2_000,
    ) -> Locator | None:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            for locator in locators:
                try:
                    locator.wait_for(state="visible", timeout=250)
                    return locator
                except PlaywrightTimeoutError:
                    continue
            time.sleep(0.15)
        return None

    def human_delay(self, min_seconds: float = 0.8, max_seconds: float = 1.8) -> None:
        time.sleep(random.uniform(min_seconds, max_seconds))

    def prompt_retry(self) -> bool:
        answer = ask_text(
            "El navegador de G4Skins sigue abierto. Pulsa Enter para reintentar o escribe 'q' para salir: ",
            title="Reintentar G4Skins",
        ).strip().lower()
        return answer not in {"q", "quit", "exit"}

    def close(self) -> None:
        if self.context is not None:
            try:
                save_session(self.context, self.session_file, self.logger)
            except Exception:
                self.logger.exception("Fallo al guardar la sesion de G4Skins durante el cierre.")
            self.context.close()
            self.context = None
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        self.page = None
