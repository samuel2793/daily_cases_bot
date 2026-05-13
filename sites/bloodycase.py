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

from .keydrop import load_session, save_balance_snapshot, save_session
from .steam import SteamAvatarManager

DEFAULT_URL = "https://bloodycase.com/es/daily-free"
SITE_NAME = "bloodycase"
STEAM_NICK_SUFFIX = "BloodyCase.com"
BALANCE_XPATH = (
    "/html/body/app-root/layout/div/blc-header/header/div/div[1]/blc-header-user/div/div[1]/div[1]/div[2]/blc-money/span"
)
CLAIM_BUTTON_XPATH = (
    "/html/body/app-root/layout/div/div/div/div/daily-free-layout/div/df-daily-cases/div/div/button[1]"
)
CLAIM_BUTTON_TEXT_XPATH = (
    "/html/body/app-root/layout/div/div/div/div/daily-free-layout/div/df-daily-cases/div/div/button[1]/div[1]/div/div[2]"
)
LOGIN_PATTERN = re.compile(r"(iniciar sesi[oó]n|login|sign in|steam)", re.IGNORECASE)
CLAIM_PATTERN = re.compile(r"\bclaim\b", re.IGNORECASE)


@dataclass(slots=True)
class BloodyCaseSite:
    session_file: Path
    steam_session_file: Path
    steam_avatar_file: Path
    balances_file: Path
    workspace_dir: Path
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
        self.diagnostics_dir = self.workspace_dir / "bloodycase_daily_free"
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> str:
        with sync_playwright() as playwright:
            self.playwright = playwright
            self._open_browser(playwright)

            try:
                while True:
                    steam_manager: SteamAvatarManager | None = None
                    try:
                        self.open_home_page()
                        self.dismiss_cookie_banner()
                        self.ensure_authenticated()
                        self.dismiss_cookie_banner()
                        balance_text = self.read_balance_text()
                        balance_value = self.parse_balance_value(balance_text)
                        self.persist_balance(balance_text, balance_value)
                        button_text = self.inspect_claim_button_text()

                        if not button_text:
                            self.capture_post_claim_diagnostics(
                                balance_text=balance_text,
                                claim_button_text=None,
                                status="button_not_found",
                            )
                            self.logger.warning(
                                "No se encontro el boton CLAIM de BloodyCase. Saldo detectado: %s",
                                balance_text,
                            )
                            return "button_not_found"

                        if not CLAIM_PATTERN.search(button_text):
                            self.capture_post_claim_diagnostics(
                                balance_text=balance_text,
                                claim_button_text=button_text,
                                status="not_claimable",
                            )
                            self.logger.info(
                                "BloodyCase no esta en estado CLAIM. Saldo detectado: %s | Estado: %s",
                                balance_text,
                                button_text,
                            )
                            return "not_claimable"

                        steam_manager = self.apply_steam_requirements()
                        self.reload_daily_free_page_after_steam_changes()
                        button_text = self.inspect_claim_button_text() or button_text
                        self.click_claim_button()
                        self.human_delay(6.0, 9.0)
                        self.capture_post_claim_diagnostics(
                            balance_text=balance_text,
                            claim_button_text=button_text,
                            status="claim_clicked",
                        )
                        self.logger.info(
                            "Flujo de BloodyCase finalizado. Saldo detectado: %s | Boton inicial: %s",
                            balance_text,
                            button_text,
                        )
                        return "claim_clicked"
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        self.logger.exception(
                            "Error controlado en BloodyCase. El navegador seguira abierto."
                        )
                        if self.context is not None:
                            save_session(self.context, self.session_file, self.logger)
                        if not self.prompt_retry():
                            return "aborted"
                    finally:
                        self.cleanup_steam_requirements(steam_manager)
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
        self.logger.info("Chromium visible iniciado para BloodyCase.")

    def open_home_page(self) -> None:
        assert self.page is not None

        self.logger.info("Abriendo %s", self.url)
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_for_page_ready()
        self.human_delay(1.2, 2.4)

    def wait_for_page_ready(self) -> None:
        assert self.page is not None

        self.page.wait_for_load_state("domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de BloodyCase no entro en networkidle. Se continua con timeout controlado."
            )

        self.page.locator("body").wait_for(state="visible", timeout=10_000)

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        if self.is_logged_in():
            self.logger.info("Sesion reutilizada automaticamente en BloodyCase.")
            return

        self.logger.warning("No hay sesion valida de BloodyCase. Se requiere login manual.")

        while True:
            input(
                "Haz login manual en BloodyCase en la ventana de Chromium y luego pulsa Enter aqui. "
            )
            self.human_delay(1.5, 2.8)
            self.page.goto(self.url, wait_until="domcontentloaded")
            self.wait_for_page_ready()

            if self.is_logged_in():
                save_session(self.context, self.session_file, self.logger)
                self.logger.info("Login manual detectado en BloodyCase y sesion guardada.")
                return

            answer = input(
                "No se pudo confirmar el login en BloodyCase. Enter para revisar otra vez o 'q' para salir: "
            ).strip().lower()
            if answer in {"q", "quit", "exit"}:
                raise RuntimeError("Login manual de BloodyCase cancelado por el usuario.")

    def is_logged_in(self) -> bool:
        assert self.page is not None

        if self.has_authenticated_signals():
            return True

        guest_locators = [
            self.page.get_by_role("button", name=LOGIN_PATTERN).first,
            self.page.get_by_role("link", name=LOGIN_PATTERN).first,
            self.page.locator("a[href*='login']").first,
            self.page.locator("a[href*='steam']").first,
            self.page.locator("button[data-testid*='login']").first,
        ]

        return self.first_visible(guest_locators, timeout_ms=1_500) is None

    def has_authenticated_signals(self) -> bool:
        assert self.page is not None

        auth_locators = [
            self.page.locator(f"xpath={BALANCE_XPATH}"),
            self.page.locator(f"xpath={CLAIM_BUTTON_XPATH}"),
            self.page.locator(f"xpath={CLAIM_BUTTON_TEXT_XPATH}"),
        ]
        return self.first_visible(auth_locators, timeout_ms=4_000) is not None

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

        self.safe_click(banner_button, "banner de cookies de BloodyCase", allow_fail=True)

    def read_balance_text(self) -> str:
        assert self.page is not None

        locators = [
            self.page.locator(f"xpath={BALANCE_XPATH}"),
        ]
        locator = self.first_visible(locators, timeout_ms=12_000)
        if locator is None:
            raise RuntimeError("No se encontro el contenedor del saldo de BloodyCase.")

        self.human_delay(0.5, 1.0)
        balance_text = (locator.text_content(timeout=5_000) or "").strip()
        if not balance_text:
            raise RuntimeError("El elemento del saldo de BloodyCase esta visible pero vacio.")

        self.logger.info("Saldo extraido desde la cabecera de BloodyCase: %s", balance_text)
        return balance_text

    def parse_balance_value(self, balance_text: str) -> float | None:
        cleaned = re.sub(r"[^\d,.\-]", "", balance_text).strip()
        if not cleaned:
            return None

        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")

        try:
            return float(cleaned)
        except ValueError:
            self.logger.warning(
                "No se pudo convertir el saldo de BloodyCase '%s' a numero.",
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
            raise RuntimeError("No se pudo persistir el saldo de BloodyCase.")

    def inspect_claim_button_text(self) -> str | None:
        assert self.page is not None

        button_locator = self.page.locator(f"xpath={CLAIM_BUTTON_XPATH}")
        text_locator = self.page.locator(f"xpath={CLAIM_BUTTON_TEXT_XPATH}")

        try:
            button_locator.wait_for(state="visible", timeout=20_000)
            text_locator.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError:
            self.logger.warning("No se encontro el boton principal de BloodyCase.")
            return None

        button_text = self.compact_text(text_locator.inner_text(timeout=5_000))
        self.logger.info("Boton principal de BloodyCase detectado con texto: %s", button_text)
        return button_text

    def click_claim_button(self) -> None:
        assert self.page is not None

        button_locator = self.page.locator(f"xpath={CLAIM_BUTTON_XPATH}")
        self.safe_click(button_locator, "boton CLAIM de BloodyCase")

    def reload_daily_free_page_after_steam_changes(self) -> None:
        assert self.page is not None

        self.logger.info(
            "Recargando BloodyCase tras los cambios temporales de Steam para que la web refresque los requisitos."
        )
        self.human_delay(2.0, 4.0)
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.wait_for_page_ready()
        self.human_delay(2.0, 4.0)

    def capture_post_claim_diagnostics(
        self,
        balance_text: str,
        claim_button_text: str | None,
        status: str,
    ) -> None:
        assert self.page is not None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_file = self.diagnostics_dir / f"bloodycase_{timestamp}.png"
        text_file = self.diagnostics_dir / f"bloodycase_{timestamp}.txt"
        json_file = self.diagnostics_dir / f"bloodycase_{timestamp}.json"

        try:
            self.page.screenshot(path=str(screenshot_file), full_page=True)
            body_text = self.page.locator("body").inner_text(timeout=10_000)
            text_file.write_text(body_text, encoding="utf-8")

            snapshot = {
                "captured_at": datetime.now().astimezone().isoformat(),
                "url": self.page.url,
                "status": status,
                "balance_text": balance_text,
                "claim_button_text": claim_button_text,
            }
            json_file.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.logger.info(
                "Diagnostico de BloodyCase guardado en %s, %s y %s.",
                screenshot_file,
                text_file,
                json_file,
            )
        except Exception:
            self.logger.exception("No se pudo capturar el diagnostico de BloodyCase.")

    def apply_steam_requirements(self) -> SteamAvatarManager:
        if not self.steam_avatar_file.exists():
            raise FileNotFoundError(
                f"No existe la imagen de avatar para Steam en BloodyCase: {self.steam_avatar_file}"
            )

        if self.playwright is None:
            raise RuntimeError("No hay instancia activa de Playwright en BloodyCase.")

        steam_manager = self.build_steam_manager()
        try:
            steam_manager.start(playwright=self.playwright)
            steam_manager.backup_and_apply_from_file(self.steam_avatar_file)
            self.logger.info("Avatar temporal de Steam aplicado para BloodyCase.")
            self.human_delay(2.0, 4.0)
            steam_manager.backup_and_apply_profile_name_prefix_suffix(
                STEAM_NICK_SUFFIX,
                prefix_length=3,
            )
            self.logger.info(
                "Nick temporal corto de Steam aplicado para BloodyCase con sufijo %s.",
                STEAM_NICK_SUFFIX,
            )
            self.human_delay(2.0, 4.0)
            return steam_manager
        except Exception:
            steam_manager.close()
            raise

    def cleanup_steam_requirements(
        self, steam_manager: SteamAvatarManager | None
    ) -> None:
        if steam_manager is None:
            return

        try:
            restored_name = steam_manager.restore_previous_profile_name()
            if not restored_name:
                self.logger.warning(
                    "No se pudo restaurar automaticamente el nick original de Steam tras BloodyCase."
                )
            restored_avatar = steam_manager.restore_previous_avatar()
            if not restored_avatar:
                self.logger.warning(
                    "No se pudo restaurar automaticamente el avatar original de Steam tras BloodyCase."
                )
        except Exception:
            self.logger.exception(
                "Fallo durante la restauracion de los cambios temporales de Steam tras BloodyCase."
            )
        finally:
            try:
                steam_manager.close()
            except Exception:
                self.logger.exception("Fallo al cerrar SteamAvatarManager tras BloodyCase.")

    def build_steam_manager(self) -> SteamAvatarManager:
        return SteamAvatarManager(
            session_file=self.steam_session_file,
            workspace_dir=self.workspace_dir,
            logger=logging.getLogger("daily_cases_bot.steam"),
        )

    def compact_text(self, value: str) -> str:
        return " ".join(value.split()).strip()

    def safe_click(
        self,
        locator: Locator,
        description: str,
        allow_fail: bool = False,
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
                    "No se pudo hacer hover sobre %s en BloodyCase. Se intenta click.",
                    description,
                )

            locator.click(timeout=10_000)
            self.logger.info("Click realizado en %s.", description)
            return True
        except Exception:
            if allow_fail:
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
        answer = input(
            "El navegador de BloodyCase sigue abierto. Pulsa Enter para reintentar o escribe 'q' para salir: "
        ).strip().lower()
        return answer not in {"q", "quit", "exit"}

    def close(self) -> None:
        if self.context is not None:
            page_closed = False
            if self.page is not None:
                try:
                    page_closed = self.page.is_closed()
                except Exception:
                    page_closed = True

            if not page_closed:
                try:
                    save_session(self.context, self.session_file, self.logger)
                except Exception:
                    self.logger.exception(
                        "Fallo al guardar la sesion de BloodyCase durante el cierre."
                    )

            try:
                self.context.close()
            except Exception:
                self.logger.exception("Fallo al cerrar el contexto de BloodyCase.")
            self.context = None
            self.page = None

        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                self.logger.exception("Fallo al cerrar Chromium de BloodyCase.")
            self.browser = None
