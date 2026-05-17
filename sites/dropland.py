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

DEFAULT_URL = "https://dropland.net/daily_free"
SITE_NAME = "dropland"
STEAM_REQUIRED_NAME = "dropland.net"
BALANCE_SELECTOR = (
    "body > header > div.header__wrapper.wrapper > div > "
    "div.header__profile-block > div.header__profile > div > div"
)
OPEN_ACTION_SELECTOR = "body > main > section.daily > div:nth-child(3) > button"
LOGIN_PATTERN = re.compile(r"(iniciar sesi[oó]n|login|sign in|steam)", re.IGNORECASE)
OPEN_PATTERN = re.compile(r"(open daily case|open case|abrir caja)", re.IGNORECASE)
COOLDOWN_PATTERN = re.compile(
    r"(\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b|available in|cooldown|wait|\b\d+\s*h\b|\b\d+\s*min\b)",
    re.IGNORECASE,
)
SELL_PATTERN = re.compile(r"(sell|vender)", re.IGNORECASE)
MONEY_BALANCE_REWARD_PATTERN = re.compile(
    r"\bMONEY\s+BALANCE\s*\$?\s*([0-9]+(?:[.,][0-9]{1,2})?)",
    re.IGNORECASE,
)
LIVE_DROP_ACTIVITY_PATTERN = re.compile(
    r"\b\d+\s+(?:minutes?|hours?)\s+ago\b",
    re.IGNORECASE,
)
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
    r"(dropland|daily free|daily case|open daily case|open case|sell|vender|upgrade|battle|"
    r"contract|daily|case|balance|wallet|deposit|free cases|faq|contact|profile|logout|"
    r"steam|more free cases|visit site|close|try again|top drop|live drop|case opened|online)",
    re.IGNORECASE,
)
POST_OPEN_MAX_ATTEMPTS = 14
POST_OPEN_REWARD_STABLE_ATTEMPTS = 2


@dataclass(slots=True)
class DroplandSite:
    session_file: Path
    steam_session_file: Path
    workspace_dir: Path
    balances_file: Path
    logger: logging.Logger
    url: str = DEFAULT_URL
    headless: bool = False
    slow_mo_ms: int = 90
    browser: Browser | None = field(default=None, init=False)
    context: BrowserContext | None = field(default=None, init=False)
    page: Page | None = field(default=None, init=False)
    playwright: Playwright | None = field(default=None, init=False)
    diagnostics_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.diagnostics_dir = self.workspace_dir / "dropland_daily_free"
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

                        button_text, button_enabled = self.inspect_open_action_state()
                        if not button_text:
                            cooldown_text = self.detect_cooldown_text_from_page()
                            if cooldown_text:
                                self.capture_diagnostics(
                                    status="cooldown",
                                    balance_text_before=balance_text,
                                    balance_value_before=balance_value,
                                    button_text=cooldown_text,
                                    reward_text=None,
                                    reward_kind="unknown",
                                    reward_candidates=[],
                                    visible_buttons=[],
                                    sell_button_text=None,
                                    sell_clicked=False,
                                    gain_value=None,
                                    observations=[],
                                )
                                self.logger.info(
                                    "Dropland muestra la daily case en cooldown/no disponible. Contador detectado: %s",
                                    cooldown_text,
                                )
                                return "cooldown"
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
                                sell_clicked=False,
                                gain_value=None,
                                observations=[],
                            )
                            return "cooldown"

                        if self.is_cooldown(button_text):
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
                                sell_clicked=False,
                                gain_value=None,
                                observations=[],
                            )
                            self.logger.info(
                                "Dropland muestra la daily case en cooldown/no disponible. Texto detectado: %s",
                                button_text,
                            )
                            return "cooldown"

                        if not button_enabled:
                            steam_manager = self.apply_steam_requirement()
                            steam_requirement_applied = True
                            button_text, button_enabled = (
                                self.reload_after_steam_change_and_reinspect()
                            )

                        if not button_text:
                            self.logger.warning(
                                "Dropland no mostro el boton principal tras recargar despues del cambio de nick."
                            )
                            return "account_setup_required"

                        if self.is_cooldown(button_text):
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
                                sell_clicked=False,
                                gain_value=None,
                                observations=[],
                            )
                            return "cooldown"

                        if not self.is_open_ready(button_text, button_enabled):
                            if not steam_requirement_applied:
                                steam_manager = self.apply_steam_requirement()
                                steam_requirement_applied = True
                                button_text, button_enabled = (
                                    self.reload_after_steam_change_and_reinspect()
                                )
                            if not self.is_open_ready(button_text, button_enabled):
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
                                    sell_clicked=False,
                                    gain_value=None,
                                    observations=[],
                                )
                                self.logger.warning(
                                    "Dropland no quedo listo para abrir tras aplicar el nick temporal de Steam. Texto: %s | Habilitado: %s",
                                    button_text,
                                    button_enabled,
                                )
                                return "account_setup_required"

                        self.click_open_action()
                        post_open_status = self.handle_post_open_state(
                            balance_text_before=balance_text,
                            balance_value_before=balance_value,
                            button_text=button_text,
                        )
                        self.logger.info(
                            "Flujo de Dropland finalizado. Saldo detectado: %s | Estado postapertura: %s",
                            balance_text,
                            post_open_status,
                        )
                        return post_open_status
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        self.logger.exception(
                            "Error controlado en Dropland. El navegador seguira abierto."
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
        self.logger.info("Chromium visible iniciado para Dropland.")

    def open_home_page(self) -> None:
        assert self.page is not None
        self.logger.info("Abriendo %s", self.url)
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_for_page_ready()
        self.human_delay(1.0, 2.0)

    def wait_for_page_ready(self) -> None:
        assert self.page is not None
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de Dropland no entro en domcontentloaded. Se continua con timeout controlado."
            )
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de Dropland no entro en networkidle. Se continua con timeout controlado."
            )
        self.page.locator("body").wait_for(state="visible", timeout=10_000)

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        had_saved_session = self.session_file.exists()
        if had_saved_session and self.is_logged_in():
            self.logger.info("Sesion reutilizada automaticamente en Dropland.")
            return

        if had_saved_session:
            self.logger.warning(
                "La sesion guardada de Dropland ya no parece valida. Se requiere login manual."
            )
        else:
            self.logger.warning(
                "No hay sesion guardada de Dropland. Se requiere login manual confirmado por el usuario."
            )
        while True:
            ask_text(
                "Haz login manual en Dropland en la ventana de Chromium. "
                "Cuando hayas terminado y quieras validar/guardar la sesion, pulsa Enter aqui. ",
                title="Login manual en Dropland",
            )
            self.human_delay(1.5, 2.8)
            self.page.goto(self.url, wait_until="domcontentloaded")
            self.wait_for_page_ready()
            if self.is_logged_in():
                save_session(self.context, self.session_file, self.logger)
                self.logger.info("Login manual detectado en Dropland y sesion guardada.")
                return

            answer = ask_text(
                "No se pudo confirmar el login en Dropland. Enter para revisar otra vez o 'q' para salir: ",
                title="Reintento de login en Dropland",
            ).strip().lower()
            if answer in {"q", "quit", "exit"}:
                raise RuntimeError("Login manual de Dropland cancelado por el usuario.")

    def is_logged_in(self) -> bool:
        assert self.page is not None

        auth_locators = [
            self.page.locator(BALANCE_SELECTOR).first,
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
                name=re.compile(r"(aceptar|accept|allow all|accept all)", re.IGNORECASE),
            ).first,
            self.page.locator("button:has-text('Aceptar')").first,
            self.page.locator("button:has-text('Accept')").first,
        ]
        banner_button = self.first_visible(cookie_locators, timeout_ms=2_500)
        if banner_button is None:
            return
        self.safe_click(banner_button, "banner de cookies de Dropland", allow_fail=True)

    def read_balance_text(self) -> str:
        assert self.page is not None
        locator = self.page.locator(BALANCE_SELECTOR).first
        locator.wait_for(state="visible", timeout=12_000)
        self.human_delay(0.5, 1.0)
        balance_text = self.compact_text(locator.text_content(timeout=5_000) or "")
        if not balance_text:
            raise RuntimeError("No se pudo leer el saldo de Dropland.")
        self.logger.info("Saldo extraido desde la cabecera de Dropland: %s", balance_text)
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
                "No se pudo convertir el saldo de Dropland '%s' a numero.",
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
            raise RuntimeError("No se pudo persistir el saldo de Dropland.")

    def inspect_open_action_state(self) -> tuple[str | None, bool]:
        button = self.find_open_action_locator(timeout_ms=12_000)
        if button is None:
            self.logger.warning("No se encontro el boton principal de Dropland.")
            return None, False
        text = self.compact_text(button.text_content(timeout=5_000) or "")
        enabled = self.locator_is_clickable(button)
        self.logger.info(
            "Boton principal de Dropland detectado con texto: %s | Habilitado: %s",
            text,
            enabled,
        )
        return text or None, enabled

    def is_cooldown(self, button_text: str) -> bool:
        return COOLDOWN_PATTERN.search(button_text or "") is not None

    def detect_cooldown_text_from_page(self) -> str | None:
        raw_body_text = self.try_read_body_text(compact=False)
        if not raw_body_text:
            return None
        return self.extract_cooldown_text(raw_body_text)

    def extract_cooldown_text(self, text: str) -> str | None:
        compact_text = self.compact_text(text)
        direct_match = re.search(r"\b(\d{1,2}:\d{2}:\d{2})\b", compact_text)
        if direct_match:
            return direct_match.group(1)

        line_tokens = [line.strip() for line in text.splitlines() if line.strip()]
        for start_index in range(len(line_tokens)):
            assembled = self.assemble_split_cooldown_tokens(line_tokens, start_index)
            if assembled:
                return assembled
        return None

    def assemble_split_cooldown_tokens(
        self,
        line_tokens: list[str],
        start_index: int,
    ) -> str | None:
        fragments: list[str] = []
        for token in line_tokens[start_index : start_index + 8]:
            if not re.fullmatch(r"\d{1,2}|:", token):
                break
            fragments.append(token)
            candidate = "".join(fragments)
            if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", candidate):
                return candidate
            if len(candidate) > 8:
                break
        return None

    def is_open_ready(self, button_text: str | None, enabled: bool) -> bool:
        if not button_text:
            return False
        if self.is_cooldown(button_text):
            return False
        return bool(OPEN_PATTERN.search(button_text)) and enabled

    def click_open_action(self) -> None:
        button = self.find_open_action_locator(timeout_ms=8_000)
        if button is None:
            raise RuntimeError("No se encontro el boton principal de Dropland para abrir la caja.")
        self.safe_click(button, "boton OPEN DAILY CASE de Dropland")

    def find_open_action_locator(self, timeout_ms: int = 3_500) -> Locator | None:
        assert self.page is not None
        action_locators = [
            self.page.locator(OPEN_ACTION_SELECTOR).first,
            self.page.get_by_role("button", name=OPEN_PATTERN).first,
            self.page.locator("button").filter(has_text=OPEN_PATTERN).first,
        ]
        return self.first_visible(action_locators, timeout_ms=timeout_ms)

    def locator_is_clickable(self, locator: Locator) -> bool:
        try:
            if not locator.is_visible():
                return False
            if not locator.is_enabled():
                return False
            disabled_attr = locator.get_attribute("disabled")
            aria_disabled = locator.get_attribute("aria-disabled")
            class_name = locator.get_attribute("class") or ""
            if disabled_attr is not None:
                return False
            if aria_disabled and aria_disabled.strip().lower() == "true":
                return False
            if "disabled" in class_name.lower():
                return False
            return True
        except Exception:
            return False

    def apply_steam_requirement(self) -> SteamAvatarManager:
        if self.playwright is None:
            raise RuntimeError("No hay instancia activa de Playwright en Dropland.")
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
                "Nick temporal corto de Steam aplicado para Dropland con sufijo %s. Resultado: %s",
                STEAM_REQUIRED_NAME,
                temporary_name,
            )
            self.human_delay(2.0, 4.0)
            return steam_manager
        except Exception:
            steam_manager.close()
            raise

    def cleanup_steam_requirement(self, steam_manager: SteamAvatarManager | None) -> None:
        if steam_manager is None:
            return
        try:
            restored = steam_manager.restore_previous_profile_name()
            if not restored:
                self.logger.warning(
                    "No se pudo restaurar automaticamente el nick original de Steam tras Dropland."
                )
        except Exception:
            self.logger.exception(
                "Fallo durante la restauracion del nick de Steam tras Dropland."
            )
        finally:
            try:
                steam_manager.close()
            except Exception:
                self.logger.exception("Fallo al cerrar SteamAvatarManager tras Dropland.")

    def reload_after_steam_change_and_reinspect(self) -> tuple[str | None, bool]:
        assert self.page is not None
        self.logger.info(
            "Recargando Dropland tras el cambio temporal del nick de Steam para refrescar los requisitos."
        )
        self.human_delay(2.0, 4.0)
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_for_page_ready()
        self.human_delay(2.0, 4.0)
        return self.inspect_open_action_state()

    def handle_post_open_state(
        self,
        *,
        balance_text_before: str,
        balance_value_before: float | None,
        button_text: str | None,
    ) -> str:
        assert self.page is not None
        self.logger.info(
            "Esperando el estado posterior a la apertura de la daily case de Dropland. Se registraran los cambios intermedios hasta confirmar recompensa y opciones de venta."
        )

        reward_text: str | None = None
        reward_kind = "unknown"
        sell_button_text: str | None = None
        sell_clicked = False
        gain_value: float | None = None
        observations: list[dict[str, Any]] = []
        reward_stable_hits = 0
        previous_signature: tuple[str | None, str, str | None] | None = None

        for attempt in range(1, POST_OPEN_MAX_ATTEMPTS + 1):
            self.human_delay(1.0, 1.8)
            body_text = self.try_read_body_text()
            reward_candidates = self.collect_visible_text_candidates()
            visible_buttons = self.collect_visible_button_texts()
            current_button_text, _ = self.inspect_open_action_state()
            sell_button_text_probe = next(
                (text for text in visible_buttons if SELL_PATTERN.search(text)),
                None,
            )
            reward_text = self.infer_reward_text(reward_candidates, body_text=body_text)
            reward_kind = self.infer_reward_kind(reward_text)

            signature = (reward_text, reward_kind, sell_button_text_probe)
            if signature == previous_signature:
                reward_stable_hits += 1
            else:
                reward_stable_hits = 1
                previous_signature = signature

            observations.append(
                {
                    "attempt": attempt,
                    "captured_at": datetime.now().astimezone().isoformat(),
                    "button_text": current_button_text,
                    "reward_text": reward_text,
                    "reward_kind": reward_kind,
                    "sell_button_text": sell_button_text_probe,
                    "visible_buttons": visible_buttons[:30],
                }
            )
            self.logger.info(
                "Estado postapertura de Dropland (%s/%s) | Boton: %s | Recompensa: %s | Tipo: %s | Vender: %s",
                attempt,
                POST_OPEN_MAX_ATTEMPTS,
                current_button_text or "sin detectar",
                reward_text or "sin detectar",
                reward_kind,
                "si" if sell_button_text_probe else "no",
            )

            if reward_text and reward_stable_hits >= POST_OPEN_REWARD_STABLE_ATTEMPTS:
                sell_button_text = sell_button_text_probe
                if reward_kind == "skin" and sell_button_text_probe and not sell_clicked:
                    sell_clicked, gain_value = self.try_click_sell_button()
                    if sell_clicked:
                        break
                if reward_kind != "unknown" or sell_button_text_probe:
                    break

            if current_button_text and self.is_cooldown(current_button_text):
                self.capture_diagnostics(
                    status="cooldown",
                    balance_text_before=balance_text_before,
                    balance_value_before=balance_value_before,
                    button_text=current_button_text,
                    reward_text=None,
                    reward_kind="unknown",
                    reward_candidates=reward_candidates,
                    visible_buttons=visible_buttons,
                    sell_button_text=sell_button_text_probe,
                    sell_clicked=False,
                    gain_value=None,
                    observations=observations,
                )
                return "cooldown"

        status = "opened_unresolved"
        if reward_text:
            status = "opened_sold" if sell_clicked else "opened_unsold"

        self.capture_diagnostics(
            status=status,
            balance_text_before=balance_text_before,
            balance_value_before=balance_value_before,
            button_text=button_text,
            reward_text=reward_text,
            reward_kind=reward_kind,
            reward_candidates=self.collect_visible_text_candidates(),
            visible_buttons=self.collect_visible_button_texts(),
            sell_button_text=sell_button_text,
            sell_clicked=sell_clicked,
            gain_value=gain_value,
            observations=observations,
        )
        return status

    def try_click_sell_button(self) -> tuple[bool, float | None]:
        assert self.page is not None
        sell_locators = [
            self.page.get_by_role("button", name=SELL_PATTERN).first,
            self.page.locator("button").filter(has_text=SELL_PATTERN).first,
            self.page.get_by_role("link", name=SELL_PATTERN).first,
        ]
        locator = self.first_visible(sell_locators, timeout_ms=4_000)
        if locator is None:
            return False, None
        clicked = self.safe_click(locator, "boton de venta de Dropland", allow_fail=True)
        if not clicked:
            return False, None
        return True, None

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
                    if (!text || text.length < 2 || text.length > 120) continue;
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
                  return candidates.slice(0, 600);
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
            money_match = MONEY_BALANCE_REWARD_PATTERN.search(body_text)
            if money_match:
                amount = money_match.group(1).replace(",", ".")
                return f"Money balance ${amount}"

        filtered: list[dict[str, Any]] = []
        for item in reward_candidates:
            text = self.compact_text(str(item.get("text", "")))
            if not text:
                continue
            if LIVE_DROP_ACTIVITY_PATTERN.search(text):
                continue
            if GENERIC_UI_TEXT_PATTERN.search(text):
                continue
            filtered.append(item)

        weapon_matches = [
            item
            for item in filtered
            if WEAPON_REWARD_PATTERN.search(str(item.get("text", "")))
        ]
        if weapon_matches:
            weapon_matches.sort(
                key=lambda item: (
                    float(item.get("y", 0) or 0),
                    -float(item.get("height", 0) or 0),
                )
            )
            return self.compact_text(str(weapon_matches[0].get("text", ""))) or None

        money_matches = [
            item
            for item in filtered
            if MONEY_BALANCE_REWARD_PATTERN.search(str(item.get("text", "")))
        ]
        if money_matches:
            money_matches.sort(
                key=lambda item: (
                    float(item.get("y", 0) or 0),
                    -float(item.get("height", 0) or 0),
                )
            )
            text = self.compact_text(str(money_matches[0].get("text", "")))
            normalized_match = MONEY_BALANCE_REWARD_PATTERN.search(text)
            if normalized_match:
                amount = normalized_match.group(1).replace(",", ".")
                return f"Money balance ${amount}"

        filtered.sort(
            key=lambda item: (
                float(item.get("y", 0) or 0),
                -float(item.get("height", 0) or 0),
            )
        )
        if not filtered:
            return None
        return self.compact_text(str(filtered[0].get("text", ""))) or None

    def infer_reward_kind(self, reward_text: str | None) -> str:
        if not reward_text:
            return "unknown"
        if MONEY_BALANCE_REWARD_PATTERN.search(reward_text):
            return "balance"
        if WEAPON_REWARD_PATTERN.search(reward_text):
            return "skin"
        return "unknown"

    def try_read_body_text(self, *, compact: bool = True) -> str | None:
        assert self.page is not None
        try:
            body_text = self.page.locator("body").inner_text(timeout=3_000)
        except Exception:
            return None
        if not compact:
            return body_text or None
        compacted = self.compact_text(body_text)
        return compacted or None

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
        sell_clicked: bool,
        gain_value: float | None,
        observations: list[dict[str, Any]],
    ) -> None:
        assert self.page is not None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_file = self.diagnostics_dir / f"dropland_{timestamp}.png"
        text_file = self.diagnostics_dir / f"dropland_{timestamp}.txt"
        json_file = self.diagnostics_dir / f"dropland_{timestamp}.json"
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
                "reward_candidates": reward_candidates[:30],
                "visible_buttons": visible_buttons[:40],
                "sell_button_text": sell_button_text,
                "sell_clicked": sell_clicked,
                "gain_value": gain_value,
                "observations": observations,
            }
            json_file.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.logger.info(
                "Diagnostico de Dropland guardado en %s, %s y %s.",
                screenshot_file,
                text_file,
                json_file,
            )
        except Exception:
            self.logger.exception("No se pudo capturar el diagnostico de Dropland.")

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
                    "No se pudo hacer hover sobre %s en Dropland. Se intenta click.",
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
            "El navegador de Dropland sigue abierto. Pulsa Enter para reintentar o escribe 'q' para salir: ",
            title="Reintentar Dropland",
        ).strip().lower()
        return answer not in {"q", "quit", "exit"}

    def close(self) -> None:
        if self.context is not None:
            try:
                save_session(self.context, self.session_file, self.logger)
            except Exception:
                self.logger.exception("Fallo al guardar la sesion de Dropland durante el cierre.")
            self.context.close()
            self.context = None
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        self.page = None
