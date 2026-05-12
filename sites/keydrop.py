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

DEFAULT_URL = "https://key-drop.com/es/daily-case"
SITE_NAME = "keydrop"
BALANCE_XPATH = (
    "//*[@id='app-root']/header/div[1]/div[2]/div[1]/div/div[3]/a/div[2]/p[1]/span/span[1]"
)
LOGIN_PATTERN = re.compile(r"(iniciar sesi[oó]n|login|sign in)", re.IGNORECASE)


def load_session(
    session_file: Path, logger: logging.Logger | None = None
) -> dict[str, Any] | None:
    if not session_file.exists():
        if logger:
            logger.info("No existe sesion previa en %s.", session_file)
        return None

    try:
        session_data = json.loads(session_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if logger:
            logger.exception("El archivo de sesion esta corrupto: %s", session_file)
        return None
    except OSError:
        if logger:
            logger.exception("No se pudo leer la sesion desde %s", session_file)
        return None

    if logger:
        logger.info("Sesion cargada desde %s.", session_file)
    return session_data


def save_session(
    context: BrowserContext,
    session_file: Path,
    logger: logging.Logger | None = None,
) -> bool:
    try:
        session_file.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(session_file))
    except Exception:
        if logger:
            logger.exception("No se pudo guardar la sesion en %s", session_file)
        return False

    if logger:
        logger.info("Sesion guardada en %s.", session_file)
    return True


def save_balance_snapshot(
    balances_file: Path,
    site_name: str,
    balance_text: str,
    balance_value: float | None,
    source_url: str,
    logger: logging.Logger | None = None,
) -> bool:
    payload = {
        "site": site_name,
        "balance_text": balance_text,
        "balance_value": balance_value,
        "source_url": source_url,
        "captured_at": datetime.now().astimezone().isoformat(),
    }

    try:
        balances_file.parent.mkdir(parents=True, exist_ok=True)

        if balances_file.exists():
            store = json.loads(balances_file.read_text(encoding="utf-8"))
        else:
            store = {}

        site_store = store.setdefault(site_name, {"latest": None, "history": []})
        site_store["latest"] = payload
        site_store["history"].append(payload)

        balances_file.write_text(
            json.dumps(store, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        if logger:
            logger.exception("No se pudo guardar el saldo en %s", balances_file)
        return False

    if logger:
        logger.info("Saldo guardado para %s en %s.", site_name, balances_file)
    return True


@dataclass(slots=True)
class KeyDropSite:
    session_file: Path
    balances_file: Path
    logger: logging.Logger
    url: str = DEFAULT_URL
    headless: bool = False
    slow_mo_ms: int = 90
    browser: Browser | None = field(default=None, init=False)
    context: BrowserContext | None = field(default=None, init=False)
    page: Page | None = field(default=None, init=False)

    def run(self) -> None:
        with sync_playwright() as playwright:
            self._open_browser(playwright)

            try:
                while True:
                    try:
                        self.open_daily_case_page()
                        self.dismiss_cookie_banner()
                        self.ensure_authenticated()
                        self.dismiss_cookie_banner()
                        balance_text = self.read_balance_text()
                        balance_value = self.parse_balance_value(balance_text)
                        self.persist_balance(balance_text, balance_value)

                        save_session(self.context, self.session_file, self.logger)
                        self.logger.info(
                            "Flujo de KeyDrop finalizado. Saldo detectado: %s",
                            balance_text,
                        )
                        return
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        self.logger.exception(
                            "Error controlado en KeyDrop. El navegador seguira abierto."
                        )
                        if self.context is not None:
                            save_session(self.context, self.session_file, self.logger)
                        if not self.prompt_retry():
                            return
            finally:
                self.close()

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
        self.logger.info("Chromium visible iniciado.")

    def open_daily_case_page(self) -> None:
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
                "La pagina no entro en networkidle. Se continua con timeout controlado."
            )

        self.page.locator("body").wait_for(state="visible", timeout=10_000)

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        if self.is_logged_in():
            self.logger.info("Sesion reutilizada automaticamente.")
            return

        self.logger.warning("No hay sesion valida. Se requiere login manual.")

        while True:
            input(
                "Haz login manual en la ventana de Chromium y luego pulsa Enter aqui. "
            )
            self.human_delay(1.5, 2.8)
            self.page.reload(wait_until="domcontentloaded")
            self.wait_for_page_ready()

            if self.is_logged_in():
                save_session(self.context, self.session_file, self.logger)
                self.logger.info("Login manual detectado y sesion guardada.")
                return

            answer = input(
                "No se pudo confirmar el login. Enter para revisar otra vez o 'q' para salir: "
            ).strip().lower()
            if answer in {"q", "quit", "exit"}:
                raise RuntimeError("Login manual cancelado por el usuario.")

    def is_logged_in(self) -> bool:
        assert self.page is not None

        guest_locators = [
            self.page.get_by_role("button", name=LOGIN_PATTERN).first,
            self.page.get_by_role("link", name=LOGIN_PATTERN).first,
            self.page.locator("a[href*='login']").first,
            self.page.locator("button[data-testid*='login']").first,
        ]

        return self.first_visible(guest_locators, timeout_ms=1_500) is None

    def dismiss_cookie_banner(self) -> None:
        assert self.page is not None

        cookie_locators = [
            self.page.get_by_role(
                "button",
                name=re.compile(r"(aceptar|accept|zgadzam|allow all)", re.IGNORECASE),
            ).first,
            self.page.locator("button:has-text('Aceptar')").first,
            self.page.locator("button:has-text('Accept')").first,
        ]

        banner_button = self.first_visible(cookie_locators, timeout_ms=2_500)
        if banner_button is None:
            return

        self.safe_click(banner_button, "banner de cookies", allow_fail=True)

    def read_balance_text(self) -> str:
        assert self.page is not None

        locator = self.page.locator(f"xpath={BALANCE_XPATH}")
        locator.wait_for(state="visible", timeout=12_000)
        self.human_delay(0.5, 1.0)

        balance_text = locator.inner_text(timeout=5_000).strip()
        if not balance_text:
            raise RuntimeError("El elemento del saldo esta visible pero vacio.")

        self.logger.info("Saldo extraido desde la cabecera de KeyDrop: %s", balance_text)
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
            self.logger.warning("No se pudo convertir el saldo '%s' a numero.", balance_text)
            return None

    def persist_balance(self, balance_text: str, balance_value: float | None) -> None:
        saved = save_balance_snapshot(
            balances_file=self.balances_file,
            site_name=SITE_NAME,
            balance_text=balance_text,
            balance_value=balance_value,
            source_url=self.url,
            logger=self.logger,
        )
        if not saved:
            raise RuntimeError("No se pudo persistir el saldo capturado.")

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
                self.logger.info("No se pudo hacer hover sobre %s. Se intenta click.", description)

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
            "El navegador sigue abierto. Pulsa Enter para reintentar o escribe 'q' para salir: "
        ).strip().lower()
        return answer not in {"q", "quit", "exit"}

    def close(self) -> None:
        if self.context is not None:
            try:
                save_session(self.context, self.session_file, self.logger)
            except Exception:
                self.logger.exception("Fallo al guardar la sesion durante el cierre.")
            self.context.close()
            self.context = None

        if self.browser is not None:
            self.browser.close()
            self.browser = None
