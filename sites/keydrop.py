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
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from interaction import ask_text

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
COOLDOWN_PATTERN = re.compile(
    r"(tiempo restante|remaining time|\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b)",
    re.IGNORECASE,
)
ZERO_COOLDOWN_PATTERN = re.compile(r"\b0{1,2}\s*:\s*0{2}\s*:\s*0{2}\b")
STEAM_REQUIREMENT_PATTERN = re.compile(r"(steam|avatar|perfil|profile|photo|foto)", re.IGNORECASE)
AVATAR_RECHECK_PATTERN = re.compile(
    r"(comprobar|check again|retry|revisar|verific|verify)",
    re.IGNORECASE,
)
AVATAR_VALID_PATTERN = re.compile(r"(avatar es v[aá]lido|avatar is valid|v[aá]lido|valid)", re.IGNORECASE)
AVATAR_INVALID_PATTERN = re.compile(
    r"(tu avatar es incorrecto|avatar incorrecto|your avatar is incorrect|invalid avatar)",
    re.IGNORECASE,
)
SELL_PATTERN = re.compile(r"(vender|sell)", re.IGNORECASE)
COINS_PATTERN = re.compile(r"gold\s*coins?|coins?|monedas?", re.IGNORECASE)
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
    r"(keydrop|daily case|daily|abrir|open|vender|sell|upgrade|mejorar|withdraw|retirar|"
    r"steam|avatar|perfil|profile|valid|invalid|tiempo restante|remaining time|balance|saldo|"
    r"check|comprobar|verificar|gratis|free|claim|cooldown)",
    re.IGNORECASE,
)
DEPOSIT_LIMIT_PATTERN = re.compile(
    r"(l[ií]mite a la cantidad de cajas diarias|sin realizar un dep[oó]sito|"
    r"para abrir m[aá]s cajas diarias|recargues tu cuenta con 10\s*\$)",
    re.IGNORECASE,
)
POST_OPEN_MAX_ATTEMPTS = 18
POST_OPEN_REWARD_STABLE_ATTEMPTS = 2


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
    playwright: Playwright | None = field(default=None, init=False)
    diagnostics_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.diagnostics_dir = self.balances_file.parent / "keydrop_daily_case"
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> str:
        with sync_playwright() as playwright:
            self.playwright = playwright
            self._open_browser(playwright)

            try:
                while True:
                    steam_manager: SteamAvatarManager | None = None
                    balance_text: str | None = None
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
                        initial_button_text = self.inspect_stable_daily_case_open_button()
                        if initial_button_text and self.is_cooldown_active(initial_button_text):
                            self.logger.info(
                                "Daily case de KeyDrop en cooldown. Saldo detectado: %s | Estado: %s",
                                balance_text,
                                initial_button_text,
                            )
                            return "cooldown"
                        steam_manager = self.prepare_daily_case_avatar_requirement()
                        button_text = self.ensure_daily_case_ready_to_open()
                        deposit_limit_text = self.detect_deposit_limit_banner_text()
                        if deposit_limit_text:
                            self.logger.warning(
                                "KeyDrop no permite abrir la caja diaria por el limite sin deposito: %s",
                                deposit_limit_text,
                            )
                            self.capture_blocked_open_diagnostics(
                                status="deposit_required",
                                balance_text_before=balance_text,
                                balance_value_before=balance_value,
                                initial_button_text=button_text,
                                restriction_text=deposit_limit_text,
                            )
                            return "deposit_required"
                        self.click_daily_case_open_button()
                        post_open_status = self.handle_post_open_state(
                            balance_text_before=balance_text,
                            balance_value_before=balance_value,
                            initial_button_text=button_text,
                        )

                        self.logger.info(
                            "Flujo de KeyDrop finalizado. Saldo detectado: %s | Boton daily case: %s | Estado postapertura: %s",
                            balance_text,
                            button_text or "no detectado",
                            post_open_status,
                        )
                        return post_open_status
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        self.logger.exception(
                            "Error controlado en KeyDrop. El navegador seguira abierto."
                        )
                        if self.context is not None:
                            save_session(self.context, self.session_file, self.logger)
                        if not self.prompt_retry():
                            return "aborted"
                    finally:
                        self.cleanup_steam_avatar_requirement(steam_manager)
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
        self.logger.info("Chromium visible iniciado.")

    def open_home_page(self) -> None:
        assert self.page is not None

        self.logger.info("Abriendo %s", self.url)
        self.navigate_with_recovery(self.url, "pagina inicial de KeyDrop")
        self.wait_for_page_ready()
        self.human_delay(1.2, 2.4)

    def wait_for_page_ready(self) -> None:
        assert self.page is not None

        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de KeyDrop no entro en domcontentloaded. Se continua con timeout controlado."
            )
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina no entro en networkidle. Se continua con timeout controlado."
            )

        self.page.locator("body").wait_for(state="visible", timeout=10_000)

    def navigate_with_recovery(
        self,
        url: str,
        description: str,
        attempts: int = 3,
    ) -> None:
        assert self.context is not None

        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            assert self.page is not None
            try:
                self.page.goto(url, wait_until="commit", timeout=20_000)
                return
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                last_error = exc
                current_url = self.safe_current_url()
                self.logger.warning(
                    "No se pudo abrir %s en KeyDrop (intento %s/%s). URL actual: %s | Error: %s",
                    description,
                    attempt,
                    attempts,
                    current_url or "sin URL",
                    exc,
                )

                if self.is_probably_on_target_url(url):
                    self.logger.info(
                        "KeyDrop parece haber empezado a navegar pese al error inicial. Se intenta continuar."
                    )
                    return

                self.reset_page_after_navigation_failure()
                self.human_delay(1.0, 2.0)

        raise RuntimeError(
            f"No se pudo abrir {description} en KeyDrop tras {attempts} intentos."
        ) from last_error

    def safe_current_url(self) -> str:
        if self.page is None:
            return ""
        try:
            return self.page.url
        except Exception:
            return ""

    def is_probably_on_target_url(self, target_url: str) -> bool:
        current_url = self.safe_current_url()
        if not current_url:
            return False

        normalized_target = target_url.rstrip("/")
        normalized_current = current_url.rstrip("/")
        if normalized_current == normalized_target:
            return True
        return normalized_current.startswith(normalized_target + "/")

    def stop_page_loading(self) -> None:
        if self.page is None:
            return

        try:
            self.page.evaluate("window.stop()")
        except Exception:
            pass

    def reset_page_after_navigation_failure(self) -> None:
        assert self.context is not None

        old_page = self.page
        try:
            if old_page is not None:
                try:
                    old_page.evaluate("window.stop()")
                except Exception:
                    pass
        except Exception:
            pass

        self.page = self.context.new_page()
        self.page.bring_to_front()
        self.logger.info(
            "Pestana de KeyDrop recreada tras un fallo de navegacion para salir de about:blank."
        )

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        if self.is_logged_in():
            self.logger.info("Sesion reutilizada automaticamente.")
            return

        self.logger.warning("No hay sesion valida. Se requiere login manual.")

        while True:
            ask_text(
                "Haz login manual en la ventana de Chromium y luego pulsa Enter aqui. ",
                title="Login manual en KeyDrop",
            )
            self.human_delay(1.5, 2.8)
            self.page.reload(wait_until="domcontentloaded")
            self.wait_for_page_ready()

            if self.is_logged_in():
                save_session(self.context, self.session_file, self.logger)
                self.logger.info("Login manual detectado y sesion guardada.")
                return

            answer = ask_text(
                "No se pudo confirmar el login. Enter para revisar otra vez o 'q' para salir: ",
                title="Reintento de login en KeyDrop",
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

        button_text = self.compact_text(text_locator.inner_text(timeout=5_000))
        if not button_text:
            self.logger.warning("El boton de apertura existe pero su texto esta vacio.")
            return ""

        self.logger.info(
            "Boton de apertura de daily case detectado con texto: %s",
            button_text,
        )
        return button_text

    def inspect_stable_daily_case_open_button(self, attempts: int = 4) -> str | None:
        button_text: str | None = None

        for attempt in range(1, attempts + 1):
            button_text = self.inspect_daily_case_open_button()
            if not button_text:
                return button_text
            if not self.is_zero_cooldown_placeholder(button_text):
                return button_text

            if attempt < attempts:
                self.logger.info(
                    "El boton de KeyDrop aun muestra 00:00:00. Esperando estabilizacion (%s/%s).",
                    attempt,
                    attempts,
                )
                self.human_delay(1.0, 1.6)

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
            raise RuntimeError(
                "Estado de comprobacion de avatar no reconocido en KeyDrop: "
                f"{avatar_check_text}"
            )

        steam_manager = self.apply_steam_avatar_requirement()
        self.click_avatar_check_button()
        self.verify_avatar_check_result()
        return steam_manager

    def ensure_daily_case_ready_to_open(self) -> str:
        button_text = self.inspect_stable_daily_case_open_button()
        if button_text and self.is_cooldown_active(button_text):
            raise RuntimeError(f"La daily case esta en cooldown: {button_text}")
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
        if self.playwright is None:
            raise RuntimeError("No hay instancia activa de Playwright en KeyDrop.")

        steam_logger = logging.getLogger("daily_cases_bot.steam")
        steam_manager = SteamAvatarManager(
            session_file=self.steam_session_file,
            workspace_dir=self.steam_workspace_dir,
            logger=steam_logger,
        )

        try:
            steam_manager.start(playwright=self.playwright)
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

    def handle_post_open_state(
        self,
        balance_text_before: str,
        balance_value_before: float | None,
        initial_button_text: str | None,
    ) -> str:
        assert self.page is not None

        self.logger.info(
            "Esperando el estado posterior a la apertura de la daily case de KeyDrop. "
            "Se registraran los cambios intermedios hasta confirmar recompensa y opciones de venta."
        )
        observations: list[dict[str, Any]] = []
        reward_stable_hits = 0
        last_signature: tuple[str | None, str, str | None] | None = None
        reward_candidates: list[dict[str, Any]] = []
        reward_text: str | None = None
        reward_kind = "unknown"
        post_open_buttons: list[str] = []
        sell_button_text: str | None = None

        for attempt in range(1, POST_OPEN_MAX_ATTEMPTS + 1):
            if attempt == 1:
                self.human_delay(2.5, 3.5)
            else:
                self.human_delay(1.0, 1.8)

            deposit_limit_text = self.detect_deposit_limit_banner_text()
            if deposit_limit_text:
                self.logger.warning(
                    "KeyDrop ha mostrado el aviso de limite sin deposito tras intentar abrir la caja: %s",
                    deposit_limit_text,
                )
                self.capture_post_open_diagnostics(
                    status="deposit_required",
                    balance_text_before=balance_text_before,
                    balance_value_before=balance_value_before,
                    balance_text_after=None,
                    balance_value_after=None,
                    initial_button_text=initial_button_text,
                    reward_text=None,
                    reward_kind="unknown",
                    reward_candidates=[],
                    visible_buttons_before_sell=[],
                    visible_buttons_after_sell=[],
                    sell_button_text=None,
                    sell_offer_value=None,
                    sell_clicked=False,
                    gain_value=None,
                    observations=observations,
                    restriction_text=deposit_limit_text,
                )
                return "deposit_required"

            reward_candidates = self.collect_visible_text_candidates()
            reward_text = self.infer_reward_text(reward_candidates)
            reward_kind = self.infer_reward_kind(reward_text)
            post_open_buttons = self.collect_visible_button_texts()
            sell_button_text = self.find_sell_button_text()
            button_text = self.try_read_daily_case_button_text()
            observation = {
                "attempt": attempt,
                "captured_at": datetime.now().astimezone().isoformat(),
                "button_text": button_text,
                "reward_text": reward_text,
                "reward_kind": reward_kind,
                "sell_button_text": sell_button_text,
                "visible_buttons": post_open_buttons,
            }
            observations.append(observation)

            signature = (reward_text, reward_kind, sell_button_text)
            if signature != last_signature:
                self.logger.info(
                    "Estado postapertura de KeyDrop (%s/%s) | Boton: %s | Recompensa: %s | Tipo: %s | Vender: %s",
                    attempt,
                    POST_OPEN_MAX_ATTEMPTS,
                    button_text or "sin texto",
                    reward_text or "sin detectar",
                    reward_kind,
                    sell_button_text or "no",
                )
                last_signature = signature

            if reward_text:
                reward_stable_hits += 1
            else:
                reward_stable_hits = 0

            if reward_kind == "skin" and sell_button_text and reward_stable_hits >= POST_OPEN_REWARD_STABLE_ATTEMPTS:
                break
            if reward_kind == "coins" and reward_stable_hits >= POST_OPEN_REWARD_STABLE_ATTEMPTS:
                break
            if reward_kind == "unknown" and sell_button_text:
                self.logger.info(
                    "KeyDrop ya muestra un boton de venta, pero la recompensa aun no es legible. Se sigue esperando."
                )

        if reward_text:
            self.logger.info(
                "Recompensa candidata detectada en KeyDrop: %s | Tipo: %s",
                reward_text,
                reward_kind,
            )
        else:
            self.logger.warning(
                "No se pudo inferir con claridad la recompensa de la daily case de KeyDrop."
            )

        if post_open_buttons:
            self.logger.info(
                "Botones visibles tras abrir la daily case de KeyDrop: %s",
                post_open_buttons,
            )
        else:
            self.logger.warning(
                "No se detectaron botones visibles tras abrir la daily case de KeyDrop."
            )

        sell_offer_value = self.parse_balance_value(sell_button_text) if sell_button_text else None
        sell_clicked = False
        balance_text_after = None
        balance_value_after = None
        gain_value = None
        final_buttons = post_open_buttons

        if reward_kind == "skin" and sell_button_text:
            self.logger.info(
                "Boton de venta detectado tras abrir la daily case de KeyDrop: %s",
                sell_button_text,
            )
            sell_clicked = self.click_sell_button()
            if sell_clicked:
                self.human_delay(3.0, 5.0)
                balance_text_after = self.try_read_balance_text()
                balance_value_after = (
                    self.parse_balance_value(balance_text_after) if balance_text_after else None
                )
                if balance_text_after and balance_value_after is not None:
                    self.persist_balance(balance_text_after, balance_value_after)
                if (
                    balance_value_before is not None
                    and balance_value_after is not None
                ):
                    gain_value = round(balance_value_after - balance_value_before, 2)
                    self.logger.info(
                        "Ganancia estimada por la venta en KeyDrop: %s",
                        gain_value,
                    )
                final_buttons = self.collect_visible_button_texts()

        elif reward_kind != "skin" and sell_button_text:
            self.logger.info(
                "Se ha detectado un boton de venta, pero la recompensa parece %s. No se intenta vender.",
                reward_kind,
            )

        if not reward_text:
            status = "opened_unresolved"
        else:
            status = "opened_sold" if sell_clicked else "opened_unsold"
        self.capture_post_open_diagnostics(
            status=status,
            balance_text_before=balance_text_before,
            balance_value_before=balance_value_before,
            balance_text_after=balance_text_after,
            balance_value_after=balance_value_after,
            initial_button_text=initial_button_text,
            reward_text=reward_text,
            reward_kind=reward_kind,
            reward_candidates=reward_candidates,
            visible_buttons_before_sell=post_open_buttons,
            visible_buttons_after_sell=final_buttons,
            sell_button_text=sell_button_text,
            sell_offer_value=sell_offer_value,
            sell_clicked=sell_clicked,
            gain_value=gain_value,
            observations=observations,
        )
        return status

    def inspect_avatar_check_button_text(self) -> str | None:
        assert self.page is not None

        button_locator = self.page.locator(f"xpath={DAILY_CASE_AVATAR_CHECK_BUTTON_XPATH}")
        text_locator = self.page.locator(f"xpath={DAILY_CASE_AVATAR_CHECK_BUTTON_TEXT_XPATH}")

        try:
            button_locator.wait_for(state="visible", timeout=5_000)
            text_locator.wait_for(state="visible", timeout=5_000)
        except PlaywrightTimeoutError:
            return None

        button_text = self.compact_text(text_locator.inner_text(timeout=5_000))
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

    def try_read_balance_text(self) -> str | None:
        try:
            return self.read_balance_text()
        except Exception:
            self.logger.warning("No se pudo releer el saldo de KeyDrop tras la apertura.")
            return None

    def try_read_daily_case_button_text(self) -> str | None:
        try:
            return self.inspect_daily_case_open_button()
        except Exception:
            return None

    def detect_deposit_limit_banner_text(self) -> str | None:
        assert self.page is not None
        try:
            body_text = self.page.locator("body").inner_text(timeout=3_000)
        except Exception:
            return None
        compact = self.compact_text(body_text)
        if not compact:
            return None
        match = DEPOSIT_LIMIT_PATTERN.search(compact)
        if not match:
            return None
        limit_start = max(0, match.start() - 80)
        limit_end = min(len(compact), match.end() + 180)
        return compact[limit_start:limit_end].strip()

    def find_sell_button_locator(self) -> Locator | None:
        assert self.page is not None

        sell_locators = [
            self.page.get_by_role("button", name=SELL_PATTERN).first,
            self.page.get_by_role("link", name=SELL_PATTERN).first,
            self.page.locator("button").filter(has_text=SELL_PATTERN).first,
            self.page.locator("a").filter(has_text=SELL_PATTERN).first,
            self.page.locator("[role='button']").filter(has_text=SELL_PATTERN).first,
        ]
        return self.first_visible(sell_locators, timeout_ms=4_000)

    def find_sell_button_text(self) -> str | None:
        locator = self.find_sell_button_locator()
        if locator is None:
            return None

        try:
            return self.compact_text(locator.inner_text(timeout=2_000))
        except Exception:
            return "boton de venta detectado sin texto legible"

    def click_sell_button(self) -> bool:
        locator = self.find_sell_button_locator()
        if locator is None:
            return False
        return self.safe_click(locator, "boton de vender recompensa", allow_fail=True)

    def collect_visible_button_texts(self) -> list[str]:
        assert self.page is not None

        try:
            raw_buttons = self.page.evaluate(
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

                  const nodes = Array.from(
                    document.querySelectorAll("button, a, [role='button']")
                  );
                  return nodes
                    .filter(isVisible)
                    .map((el) => (el.innerText || el.textContent || "").trim())
                    .filter((text) => text.length > 0)
                    .slice(0, 40);
                }
                """
            )
        except Exception:
            self.logger.warning("No se pudieron recolectar los botones visibles de KeyDrop.")
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_buttons or []:
            text = self.compact_text(str(item))
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    def collect_visible_text_candidates(self) -> list[dict[str, Any]]:
        assert self.page is not None

        try:
            raw_candidates = self.page.evaluate(
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

                  const nodes = Array.from(
                    document.querySelectorAll(
                      "h1, h2, h3, h4, p, span, div, strong, b"
                    )
                  );

                  return nodes
                    .filter(isVisible)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      const style = window.getComputedStyle(el);
                      return {
                        text: (el.innerText || el.textContent || "").trim(),
                        tag: el.tagName.toLowerCase(),
                        className: typeof el.className === "string" ? el.className : "",
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: rect.height,
                        fontSize: parseFloat(style.fontSize || "0"),
                        fontWeight: String(style.fontWeight || ""),
                      };
                    })
                    .filter((item) => item.text.length > 1)
                    .slice(0, 200);
                }
                """
            )
        except Exception:
            self.logger.warning("No se pudieron recolectar los textos visibles de KeyDrop.")
            return []

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_candidates or []:
            text = self.compact_text(str(item.get("text", "")))
            if not text or text in seen:
                continue
            seen.add(text)
            candidate = dict(item)
            candidate["text"] = text
            candidates.append(candidate)
        return candidates

    def infer_reward_text(self, candidates: list[dict[str, Any]]) -> str | None:
        skin_reward = self.infer_skin_reward_text(candidates)
        if skin_reward:
            return skin_reward

        coin_reward = self.infer_coin_reward_text(candidates)
        if coin_reward:
            return coin_reward

        filtered: list[dict[str, Any]] = []
        for candidate in candidates:
            text = str(candidate.get("text", "")).strip()
            if not text:
                continue
            if len(text) > 100:
                continue
            if GENERIC_UI_TEXT_PATTERN.search(text):
                continue
            if re.fullmatch(r"[\d\s.,:$€£-]+", text):
                continue
            filtered.append(candidate)

        if not filtered:
            return None

        filtered.sort(
            key=lambda item: (
                float(item.get("fontSize", 0) or 0),
                float(item.get("width", 0) or 0) * float(item.get("height", 0) or 0),
            ),
            reverse=True,
        )
        return str(filtered[0].get("text", "")).strip() or None

    def infer_skin_reward_text(self, candidates: list[dict[str, Any]]) -> str | None:
        sell_locator = self.find_sell_button_locator()
        if sell_locator is None:
            return None

        try:
            sell_box = sell_locator.bounding_box()
        except Exception:
            return None

        if not sell_box:
            return None

        sell_center_x = float(sell_box["x"]) + (float(sell_box["width"]) / 2)
        sell_top_y = float(sell_box["y"])
        nearby_candidates: list[dict[str, Any]] = []

        for candidate in candidates:
            text = str(candidate.get("text", "")).strip()
            if not text or len(text) > 80:
                continue
            if re.fullmatch(r"[\d\s.,:$€£-]+", text):
                continue
            if COINS_PATTERN.search(text):
                continue
            if SELL_PATTERN.search(text):
                continue
            if re.search(r"\b(nivel|volver|mejorar|online|pausar|saldo)\b", text, re.IGNORECASE):
                continue

            candidate_x = float(candidate.get("x", 0) or 0)
            candidate_y = float(candidate.get("y", 0) or 0)
            candidate_width = float(candidate.get("width", 0) or 0)
            candidate_center_x = candidate_x + (candidate_width / 2)

            if candidate_y > sell_top_y + 15:
                continue
            if sell_top_y - candidate_y > 260:
                continue
            if abs(candidate_center_x - sell_center_x) > 220:
                continue

            nearby_candidates.append(candidate)

        weapon_candidates = [
            candidate
            for candidate in nearby_candidates
            if WEAPON_REWARD_PATTERN.search(str(candidate.get("text", "")))
        ]
        if not weapon_candidates:
            return None

        weapon_candidates.sort(
            key=lambda item: (
                abs(
                    (
                        float(item.get("x", 0) or 0)
                        + (float(item.get("width", 0) or 0) / 2)
                    )
                    - sell_center_x
                ),
                abs(sell_top_y - float(item.get("y", 0) or 0)),
                -float(item.get("fontSize", 0) or 0),
            )
        )
        weapon_text = str(weapon_candidates[0].get("text", "")).strip()
        weapon_y = float(weapon_candidates[0].get("y", 0) or 0)
        weapon_center_x = float(weapon_candidates[0].get("x", 0) or 0) + (
            float(weapon_candidates[0].get("width", 0) or 0) / 2
        )

        finish_candidates = []
        for candidate in nearby_candidates:
            text = str(candidate.get("text", "")).strip()
            if not text or text == weapon_text:
                continue
            if WEAPON_REWARD_PATTERN.search(text):
                continue
            if len(text) > 40:
                continue

            candidate_y = float(candidate.get("y", 0) or 0)
            candidate_center_x = float(candidate.get("x", 0) or 0) + (
                float(candidate.get("width", 0) or 0) / 2
            )
            if abs(candidate_y - weapon_y) > 90:
                continue
            if abs(candidate_center_x - weapon_center_x) > 140:
                continue
            finish_candidates.append(candidate)

        if not finish_candidates:
            return weapon_text

        finish_candidates.sort(
            key=lambda item: (
                abs(float(item.get("y", 0) or 0) - weapon_y),
                abs(
                    (
                        float(item.get("x", 0) or 0)
                        + (float(item.get("width", 0) or 0) / 2)
                    )
                    - weapon_center_x
                ),
            )
        )
        finish_text = self.normalize_skin_finish(
            str(finish_candidates[0].get("text", "")).strip()
        )
        return f"{weapon_text} | {finish_text}"

    def infer_coin_reward_text(self, candidates: list[dict[str, Any]]) -> str | None:
        coin_candidates = [
            candidate
            for candidate in candidates
            if COINS_PATTERN.search(str(candidate.get("text", "")))
            and len(str(candidate.get("text", "")).strip()) <= 40
        ]
        amount_candidates = [
            candidate
            for candidate in candidates
            if re.fullmatch(r"\d{1,6}", str(candidate.get("text", "")).strip())
            and len(str(candidate.get("text", "")).strip()) <= 8
        ]

        for coin_candidate in coin_candidates:
            coin_text = self.normalize_reward_label(str(coin_candidate.get("text", "")))
            coin_x = float(coin_candidate.get("x", 0) or 0)
            coin_y = float(coin_candidate.get("y", 0) or 0)
            best_amount: str | None = None
            best_distance = float("inf")

            for amount_candidate in amount_candidates:
                amount_text = str(amount_candidate.get("text", "")).strip()
                amount_x = float(amount_candidate.get("x", 0) or 0)
                amount_y = float(amount_candidate.get("y", 0) or 0)
                distance = abs(amount_y - coin_y) + abs(amount_x - coin_x)

                if abs(amount_y - coin_y) > 180:
                    continue
                if abs(amount_x - coin_x) > 250:
                    continue
                if distance < best_distance:
                    best_distance = distance
                    best_amount = amount_text

            if best_amount:
                return f"{coin_text} {best_amount}"
        return None

    def normalize_reward_label(self, text: str) -> str:
        compact = self.compact_text(text)
        if COINS_PATTERN.search(compact):
            return "Gold Coins"
        return compact

    def normalize_skin_finish(self, text: str) -> str:
        compact = self.compact_text(text)
        if not compact:
            return compact
        if compact.upper() == compact:
            return compact.lower().title()
        return compact

    def infer_reward_kind(self, reward_text: str | None) -> str:
        if not reward_text:
            return "unknown"
        if COINS_PATTERN.search(reward_text):
            return "coins"
        if WEAPON_REWARD_PATTERN.search(reward_text):
            return "skin"
        return "unknown"

    def capture_post_open_diagnostics(
        self,
        *,
        status: str,
        balance_text_before: str,
        balance_value_before: float | None,
        balance_text_after: str | None,
        balance_value_after: float | None,
        initial_button_text: str | None,
        reward_text: str | None,
        reward_kind: str,
        reward_candidates: list[dict[str, Any]],
        visible_buttons_before_sell: list[str],
        visible_buttons_after_sell: list[str],
        sell_button_text: str | None,
        sell_offer_value: float | None,
        sell_clicked: bool,
        gain_value: float | None,
        observations: list[dict[str, Any]] | None = None,
        restriction_text: str | None = None,
    ) -> None:
        assert self.page is not None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_file = self.diagnostics_dir / f"keydrop_{timestamp}.png"
        text_file = self.diagnostics_dir / f"keydrop_{timestamp}.txt"
        json_file = self.diagnostics_dir / f"keydrop_{timestamp}.json"

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
                "balance_text_after": balance_text_after,
                "balance_value_after": balance_value_after,
                "initial_button_text": initial_button_text,
                "reward_text": reward_text,
                "reward_kind": reward_kind,
                "reward_candidates": reward_candidates[:25],
                "visible_buttons_before_sell": visible_buttons_before_sell,
                "visible_buttons_after_sell": visible_buttons_after_sell,
                "sell_button_text": sell_button_text,
                "sell_offer_value": sell_offer_value,
                "sell_clicked": sell_clicked,
                "gain_value": gain_value,
                "observations": observations or [],
                "restriction_text": restriction_text,
            }
            json_file.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.logger.info(
                "Diagnostico postapertura de KeyDrop guardado en %s, %s y %s.",
                screenshot_file,
                text_file,
                json_file,
            )
        except Exception:
            self.logger.exception("No se pudo capturar el diagnostico postapertura de KeyDrop.")

    def capture_blocked_open_diagnostics(
        self,
        *,
        status: str,
        balance_text_before: str,
        balance_value_before: float | None,
        initial_button_text: str | None,
        restriction_text: str | None,
    ) -> None:
        reward_candidates = self.collect_visible_text_candidates()
        visible_buttons = self.collect_visible_button_texts()
        self.capture_post_open_diagnostics(
            status=status,
            balance_text_before=balance_text_before,
            balance_value_before=balance_value_before,
            balance_text_after=None,
            balance_value_after=None,
            initial_button_text=initial_button_text,
            reward_text=None,
            reward_kind="unknown",
            reward_candidates=reward_candidates,
            visible_buttons_before_sell=visible_buttons,
            visible_buttons_after_sell=visible_buttons,
            sell_button_text=None,
            sell_offer_value=None,
            sell_clicked=False,
            gain_value=None,
            observations=[],
            restriction_text=restriction_text,
        )

    def is_ready_to_open(self, button_text: str) -> bool:
        if self.is_cooldown_active(button_text):
            return False
        if STEAM_REQUIREMENT_PATTERN.search(button_text):
            return False
        return bool(READY_TO_OPEN_PATTERN.search(button_text))

    def is_cooldown_active(self, button_text: str) -> bool:
        return bool(COOLDOWN_PATTERN.search(button_text))

    def is_zero_cooldown_placeholder(self, button_text: str) -> bool:
        return bool(COOLDOWN_PATTERN.search(button_text) and ZERO_COOLDOWN_PATTERN.search(button_text))

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
        answer = ask_text(
            "El navegador sigue abierto. Pulsa Enter para reintentar o escribe 'q' para salir: ",
            title="Reintentar KeyDrop",
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
