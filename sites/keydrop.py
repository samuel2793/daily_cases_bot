from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.request
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

from .steam import SteamAvatarManager

DEFAULT_URL = "https://key-drop.com/es"
SITE_NAME = "keydrop"
BALANCE_XPATH = (
    "//*[@id='app-root']/header/div[1]/div[2]/div[1]/div/div[3]/a/div[2]/p[1]/span/span[1]"
)
DAILY_CASE_ENTRY_XPATH = "//*[@id='app-root']/header/div[1]/div[1]/a[1]"
FIRST_DAILY_CASE_XPATH = "//*[@id='daily-case-grid-list']/li[1]/div/a/img[2]"
DAILY_CASE_OPEN_BUTTON_XPATH = "//*[@id='main-view']/div/div/section/div[2]/button"
DAILY_CASE_OPEN_BUTTON_TEXT_XPATH = (
    "//*[@id='main-view']/div/div/section/div[2]/button/span[2]"
)
DAILY_CASE_AVATAR_CHECK_BUTTON_XPATH = (
    "//*[@id='main-view']/div/section[1]/div/div/div[2]/div[1]/button"
)
DAILY_CASE_AVATAR_CHECK_BUTTON_TEXT_XPATH = (
    "//*[@id='main-view']/div/section[1]/div/div/div[2]/div[1]/button/span"
)
LOGIN_PATTERN = re.compile(r"(iniciar sesi[oó]n|login|sign in)", re.IGNORECASE)
READY_TO_OPEN_PATTERN = re.compile(r"(abrir|open)", re.IGNORECASE)
STEAM_REQUIREMENT_PATTERN = re.compile(r"(steam|avatar|perfil|profile|photo|foto)", re.IGNORECASE)
AVATAR_RECHECK_PATTERN = re.compile(r"(comprobar|check again|retry|revisar)", re.IGNORECASE)
AVATAR_VALID_PATTERN = re.compile(r"(avatar es v[aá]lido|avatar is valid|v[aá]lido|valid)", re.IGNORECASE)
AVATAR_INVALID_PATTERN = re.compile(
    r"(tu avatar es incorrecto|avatar incorrecto|your avatar is incorrect|invalid avatar)",
    re.IGNORECASE,
)


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
    steam_session_file: Path
    steam_avatar_file: Path
    steam_workspace_dir: Path
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
                    steam_manager: SteamAvatarManager | None = None
                    try:
                        self.open_home_page()
                        self.dismiss_cookie_banner()
                        self.ensure_authenticated()
                        self.dismiss_cookie_banner()
                        balance_text = self.read_balance_text()
                        balance_value = self.parse_balance_value(balance_text)
                        self.persist_balance(balance_text, balance_value)
                        self.open_daily_case_overview()
                        self.open_first_daily_case_level()
                        steam_manager = self.prepare_daily_case_avatar_requirement()
                        button_text = self.ensure_daily_case_ready_to_open()
                        self.click_daily_case_open_button()
                        self.human_delay(2.0, 4.0)

                        save_session(self.context, self.session_file, self.logger)
                        self.logger.info(
                            "Flujo de KeyDrop finalizado. Saldo detectado: %s | Boton daily case: %s",
                            balance_text,
                            button_text or "no detectado",
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
                        self.cleanup_steam_avatar_requirement(steam_manager)
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
            source_url=self.page.url if self.page is not None else self.url,
            logger=self.logger,
        )
        if not saved:
            raise RuntimeError("No se pudo persistir el saldo capturado.")

    def open_daily_case_overview(self) -> None:
        assert self.page is not None

        daily_case_link = self.page.locator(f"xpath={DAILY_CASE_ENTRY_XPATH}")
        self.safe_click(daily_case_link, "enlace de cabecera a daily case")
        self.page.wait_for_url(re.compile(r".*/daily-case(?:[/?#].*)?$"), timeout=15_000)
        self.wait_for_page_ready()
        self.human_delay(1.0, 1.8)

    def open_first_daily_case_level(self) -> None:
        assert self.page is not None

        first_case = self.page.locator(f"xpath={FIRST_DAILY_CASE_XPATH}")
        self.safe_click(first_case, "primera daily case")
        self.page.wait_for_url(re.compile(r".*/daily-case/level/0(?:[/?#].*)?$"), timeout=15_000)
        self.wait_for_page_ready()
        self.human_delay(1.0, 1.8)

    def get_first_daily_case_image_url(self) -> str:
        assert self.page is not None

        image_locator = self.page.locator(f"xpath={FIRST_DAILY_CASE_XPATH}")
        image_locator.wait_for(state="visible", timeout=10_000)
        image_url = image_locator.get_attribute("src")
        if not image_url:
            raise RuntimeError("La imagen de la primera daily case no tiene src.")

        self.logger.info("Imagen de la primera daily case detectada: %s", image_url)
        return image_url

    def download_first_daily_case_image(self, target_path: Path) -> Path:
        image_url = self.get_first_daily_case_image_url()
        request = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=30) as response:
            target_path.write_bytes(response.read())

        self.logger.info("Imagen de daily case descargada a %s.", target_path)
        return target_path

    def inspect_daily_case_open_button(self) -> str | None:
        assert self.page is not None

        button_locator = self.page.locator(f"xpath={DAILY_CASE_OPEN_BUTTON_XPATH}")
        text_locator = self.page.locator(f"xpath={DAILY_CASE_OPEN_BUTTON_TEXT_XPATH}")

        try:
            button_locator.wait_for(state="visible", timeout=12_000)
            text_locator.wait_for(state="visible", timeout=12_000)
        except PlaywrightTimeoutError:
            self.logger.warning("No se encontro el boton de apertura de la daily case.")
            return None

        button_text = text_locator.inner_text(timeout=5_000).strip()
        if not button_text:
            self.logger.warning("El boton de apertura existe pero su texto esta vacio.")
            return ""

        self.logger.info(
            "Boton de apertura de daily case detectado con texto: %s",
            button_text,
        )
        return button_text

    def prepare_daily_case_avatar_requirement(self) -> SteamAvatarManager | None:
        avatar_check_text = self.inspect_avatar_check_button_text()
        if not avatar_check_text:
            self.logger.info(
                "No se detecto boton de comprobacion de avatar. Se sigue con el flujo actual."
            )
            return None

        if AVATAR_VALID_PATTERN.search(avatar_check_text):
            self.logger.info("El avatar ya figura como valido en KeyDrop: %s", avatar_check_text)
            return None

        if not AVATAR_RECHECK_PATTERN.search(avatar_check_text):
            self.logger.info(
                "El boton de comprobacion de avatar no requiere accion adicional: %s",
                avatar_check_text,
            )
            return None

        steam_manager = self.apply_steam_avatar_requirement()
        self.click_avatar_check_button()
        self.verify_avatar_check_result()
        return steam_manager

    def ensure_daily_case_ready_to_open(self) -> str:
        button_text = self.inspect_daily_case_open_button()
        if button_text and self.is_ready_to_open(button_text):
            return button_text

        raise RuntimeError(
            f"La daily case sigue sin estar lista para abrir. Texto actual del boton: {button_text or 'sin texto'}"
        )

    def apply_steam_avatar_requirement(self) -> SteamAvatarManager:
        if not self.steam_avatar_file.exists():
            raise FileNotFoundError(
                f"No existe la imagen de avatar para Steam: {self.steam_avatar_file}"
            )

        steam_logger = logging.getLogger("daily_cases_bot.steam")
        steam_manager = SteamAvatarManager(
            session_file=self.steam_session_file,
            workspace_dir=self.steam_workspace_dir,
            logger=steam_logger,
        )

        try:
            steam_manager.start()
            steam_manager.backup_and_apply_from_file(self.steam_avatar_file)
            self.logger.info("Avatar temporal de Steam aplicado para desbloquear la daily case.")
            self.human_delay(2.0, 4.0)
            return steam_manager
        except Exception:
            steam_manager.close()
            raise

    def cleanup_steam_avatar_requirement(
        self, steam_manager: SteamAvatarManager | None
    ) -> None:
        if steam_manager is None:
            return

        try:
            restored = steam_manager.restore_previous_avatar()
            if not restored:
                self.logger.warning(
                    "No se pudo restaurar automaticamente el avatar original de Steam."
                )
        finally:
            steam_manager.close()

    def click_daily_case_open_button(self) -> None:
        assert self.page is not None

        button_locator = self.page.locator(f"xpath={DAILY_CASE_OPEN_BUTTON_XPATH}")
        self.safe_click(button_locator, "boton de apertura de daily case")

    def inspect_avatar_check_button_text(self) -> str | None:
        assert self.page is not None

        button_locator = self.page.locator(f"xpath={DAILY_CASE_AVATAR_CHECK_BUTTON_XPATH}")
        text_locator = self.page.locator(f"xpath={DAILY_CASE_AVATAR_CHECK_BUTTON_TEXT_XPATH}")

        try:
            button_locator.wait_for(state="visible", timeout=5_000)
            text_locator.wait_for(state="visible", timeout=5_000)
        except PlaywrightTimeoutError:
            return None

        button_text = text_locator.inner_text(timeout=5_000).strip()
        self.logger.info("Boton de comprobacion de avatar detectado con texto: %s", button_text)
        return button_text

    def click_avatar_check_button(self) -> None:
        assert self.page is not None

        button_locator = self.page.locator(f"xpath={DAILY_CASE_AVATAR_CHECK_BUTTON_XPATH}")
        self.safe_click(button_locator, "boton de comprobacion de avatar")
        self.human_delay(2.0, 4.0)

    def verify_avatar_check_result(self) -> None:
        assert self.page is not None

        if self.detect_avatar_invalid_toast():
            raise RuntimeError("KeyDrop ha indicado que el avatar de Steam es incorrecto.")

        button_text = self.inspect_avatar_check_button_text()
        if button_text and AVATAR_VALID_PATTERN.search(button_text):
            self.logger.info("KeyDrop confirmo el avatar de Steam como valido.")
            return

        raise RuntimeError(
            f"No se pudo confirmar la validez del avatar en KeyDrop. Estado actual: {button_text or 'sin texto'}"
        )

    def detect_avatar_invalid_toast(self) -> bool:
        assert self.page is not None

        toast_locators = [
            self.page.get_by_text(AVATAR_INVALID_PATTERN).first,
            self.page.locator("[class*='toast']:has-text('avatar')").first,
            self.page.locator("[role='alert']:has-text('avatar')").first,
        ]
        toast = self.first_visible(toast_locators, timeout_ms=4_000)
        if toast is None:
            return False

        try:
            toast_text = toast.inner_text(timeout=2_000).strip()
        except Exception:
            toast_text = "toast de avatar incorrecto detectado"

        self.logger.warning("Toast detectado en KeyDrop: %s", toast_text)
        return True

    def is_ready_to_open(self, button_text: str) -> bool:
        if STEAM_REQUIREMENT_PATTERN.search(button_text):
            return False
        return bool(READY_TO_OPEN_PATTERN.search(button_text))

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
