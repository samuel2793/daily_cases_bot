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

from .keydrop import load_session, save_session

DEFAULT_URL = "https://cs2.free/free-cases/"
SITE_NAME = "cs2free"
SESSION_STORAGE_ORIGIN = "https://cs2.free"
OPEN_BUTTON_XPATH = "//*[@id='__next']/section/main/div/div[2]/section/section/section/section[2]/div[1]/button"
LOGIN_PATTERN = re.compile(r"(iniciar sesi[oó]n|log in|login|sign in)", re.IGNORECASE)
AUTHENTICATED_PATTERN = re.compile(
    r"(logged in as:|profile|logout|change password|view profile)",
    re.IGNORECASE,
)
OPEN_READY_PATTERN = re.compile(r"\bopen\b", re.IGNORECASE)
COOLDOWN_PATTERN = re.compile(r"\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b")
REQUIREMENT_LABELS = [
    "Login",
    "Join Discord Server",
    "Have Steam Linked",
]
CHECK_MARK = "✅"
CROSS_MARK = "❌"
SELL_PATTERN = re.compile(r"(vender|sell)", re.IGNORECASE)
NO_REWARD_PATTERN = re.compile(
    r"(no luck today|you didn't win anything|nothing\b)",
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
    r"(cs2\.free|free cases|open|sell|vender|claim|login|discord|steam linked|"
    r"have steam linked|join discord server|how it works|bonus|free case|"
    r"daily|cases|withdraw|upgrade|battle|contract|sign in|in this case|"
    r"here's a list of the items you can win spinning this case)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class CS2FreeSite:
    session_file: Path
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
    loaded_storage_state: dict[str, Any] | None = field(default=None, init=False)
    loaded_session_storage: dict[str, dict[str, str]] = field(
        default_factory=dict,
        init=False,
    )

    def __post_init__(self) -> None:
        self.diagnostics_dir = self.workspace_dir / "cs2free_daily"
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> str:
        with sync_playwright() as playwright:
            self.playwright = playwright
            self._open_browser(playwright)

            try:
                while True:
                    try:
                        self.open_home_page()
                        self.ensure_authenticated()
                        requirement_statuses = self.inspect_account_requirements()

                        failing_requirements = [
                            label
                            for label, status in requirement_statuses.items()
                            if status == "missing"
                        ]
                        if failing_requirements:
                            self.capture_diagnostics(
                                status="account_setup_required",
                                requirement_statuses=requirement_statuses,
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons_before_sell=[],
                                visible_buttons_after_sell=[],
                                sell_button_text=None,
                                sell_clicked=False,
                            )
                            self.logger.warning(
                                "CS2.free requiere configurar la cuenta antes de abrir cajas. Requisitos pendientes: %s",
                                ", ".join(failing_requirements),
                            )
                            self.logger.warning(
                                "Configura manualmente esos requisitos en CS2.free y vuelve a ejecutar."
                            )
                            return "account_setup_required"

                        self.logger.info(
                            "CS2.free listo para continuar. Requisitos detectados: %s",
                            requirement_statuses,
                        )
                        button_text, button_enabled = self.inspect_open_button_state()
                        if button_text and (
                            self.is_open_button_cooldown(button_text, button_enabled)
                        ):
                            self.capture_diagnostics(
                                status="cooldown",
                                requirement_statuses=requirement_statuses,
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons_before_sell=[],
                                visible_buttons_after_sell=[],
                                sell_button_text=None,
                                sell_clicked=False,
                            )
                            self.logger.info(
                                "CS2.free muestra la caja en cooldown/no disponible. Texto detectado: %s",
                                button_text,
                            )
                            return "cooldown"
                        self.click_open_button()
                        post_open_status = self.handle_post_open_state(requirement_statuses)
                        self.logger.info(
                            "Flujo de CS2.free finalizado. Estado postapertura: %s",
                            post_open_status,
                        )
                        return post_open_status
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        self.logger.exception(
                            "Error controlado en CS2.free. El navegador seguira abierto."
                        )
                        if self.context is not None:
                            self.save_cs2free_session()
                        if not self.prompt_retry():
                            return "aborted"
            finally:
                self.close()
                self.playwright = None

    def _open_browser(self, playwright: Playwright) -> None:
        session_payload = self.load_cs2free_session()
        self.loaded_storage_state = session_payload["storage_state"]
        self.loaded_session_storage = session_payload["session_storage"]

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

        if self.loaded_storage_state:
            context_kwargs["storage_state"] = self.loaded_storage_state

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
        if self.loaded_session_storage:
            self.context.add_init_script(
                """
                (storageByOrigin) => {
                  const entries = storageByOrigin[window.location.origin];
                  if (!entries) return;
                  for (const [key, value] of Object.entries(entries)) {
                    try {
                      window.sessionStorage.setItem(key, value);
                    } catch (error) {
                    }
                  }
                }
                """,
                self.loaded_session_storage,
            )

        self.page = self.context.new_page()
        self.page.bring_to_front()
        self.logger.info("Chromium visible iniciado para CS2.free.")

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
                "La pagina de CS2.free no entro en domcontentloaded. Se continua con timeout controlado."
            )
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de CS2.free no entro en networkidle. Se continua con timeout controlado."
            )

        self.page.locator("body").wait_for(state="visible", timeout=10_000)

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        if self.is_logged_in():
            self.logger.info("Sesion reutilizada automaticamente en CS2.free.")
            return

        self.logger.warning("No hay sesion valida de CS2.free. Se requiere login manual.")

        while True:
            input(
                "Haz login manual en CS2.free en la ventana de Chromium y luego pulsa Enter aqui. "
            )
            self.human_delay(1.5, 2.8)
            self.page.goto(self.url, wait_until="domcontentloaded")
            self.wait_for_page_ready()

            if self.is_logged_in():
                self.save_cs2free_session(force=True)
                self.logger.info("Login manual detectado en CS2.free y sesion guardada.")
                return

            answer = input(
                "No se pudo confirmar el login en CS2.free. Enter para revisar otra vez o 'q' para salir: "
            ).strip().lower()
            if answer in {"q", "quit", "exit"}:
                raise RuntimeError("Login manual de CS2.free cancelado por el usuario.")

    def is_logged_in(self) -> bool:
        assert self.page is not None

        if self.has_authenticated_signals():
            return True

        guest_locators = [
            self.page.get_by_role("button", name=LOGIN_PATTERN).first,
            self.page.get_by_role("link", name=LOGIN_PATTERN).first,
            self.page.locator("a[href*='login']").first,
            self.page.locator("button[data-testid*='login']").first,
        ]

        if self.first_visible(guest_locators, timeout_ms=1_500) is not None:
            return False

        return True

    def has_authenticated_signals(self) -> bool:
        assert self.page is not None

        try:
            body_text = self.page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return False

        compact = self.compact_text(body_text)
        return bool(AUTHENTICATED_PATTERN.search(compact))

    def page_contains_requirement_panel(self) -> bool:
        assert self.page is not None

        try:
            body_text = self.page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return False

        compact = self.compact_text(body_text)
        return all(label in compact for label in REQUIREMENT_LABELS)

    def load_cs2free_session(self) -> dict[str, Any]:
        session_data = load_session(self.session_file, self.logger)
        if not session_data:
            return {
                "storage_state": None,
                "session_storage": {},
            }

        if "storage_state" in session_data or "session_storage" in session_data:
            return {
                "storage_state": session_data.get("storage_state"),
                "session_storage": session_data.get("session_storage") or {},
            }

        return {
            "storage_state": session_data,
            "session_storage": {},
        }

    def read_session_storage_entries(self) -> dict[str, str]:
        assert self.page is not None

        try:
            storage_entries = self.page.evaluate(
                """
                () => {
                  const entries = {};
                  for (let i = 0; i < window.sessionStorage.length; i++) {
                    const key = window.sessionStorage.key(i);
                    entries[key] = window.sessionStorage.getItem(key);
                  }
                  return entries;
                }
                """
            )
        except Exception:
            return {}

        if not isinstance(storage_entries, dict):
            return {}
        return {str(key): str(value) for key, value in storage_entries.items()}

    def save_cs2free_session(self, force: bool = False) -> bool:
        assert self.context is not None

        authenticated_now = False
        if self.page is not None:
            try:
                authenticated_now = self.has_authenticated_signals()
            except Exception:
                authenticated_now = False

        if not force and not authenticated_now:
            self.logger.info(
                "No se sobreescribe la sesion de CS2.free porque el estado actual no parece autenticado."
            )
            return False

        try:
            storage_state = self.context.storage_state(indexed_db=True)
        except Exception:
            self.logger.exception("No se pudo leer el storage_state de CS2.free.")
            return False

        session_storage_entries = {}
        if self.page is not None:
            session_storage_entries = self.read_session_storage_entries()

        payload = {
            "storage_state": storage_state,
            "session_storage": (
                {SESSION_STORAGE_ORIGIN: session_storage_entries}
                if session_storage_entries
                else {}
            ),
        }

        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.session_file.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            self.logger.exception("No se pudo guardar la sesion extendida de CS2.free.")
            return False

        self.logger.info("Sesion guardada en %s.", self.session_file)
        return True

    def inspect_account_requirements(self) -> dict[str, str]:
        assert self.page is not None

        deadline = time.monotonic() + 14.0
        last_statuses = self.read_account_requirements_once()
        clear_samples = 0
        last_missing: list[str] = []

        while True:
            missing_requirements = [
                label for label, status in last_statuses.items() if status == "missing"
            ]

            if missing_requirements:
                if missing_requirements != last_missing:
                    self.logger.info(
                        "CS2.free aun muestra requisitos pendientes mientras carga la cuenta. "
                        "Se espera un poco mas. Pendientes: %s",
                        ", ".join(missing_requirements),
                    )
                clear_samples = 0
                last_missing = missing_requirements
            else:
                clear_samples += 1
                if clear_samples >= 2:
                    self.logger.info(
                        "Estado de requisitos detectado en CS2.free: %s",
                        last_statuses,
                    )
                    return last_statuses

            if time.monotonic() >= deadline:
                self.logger.info(
                    "Estado de requisitos detectado en CS2.free tras esperar la estabilizacion: %s",
                    last_statuses,
                )
                return last_statuses

            time.sleep(1.0)
            last_statuses = self.read_account_requirements_once()

    def read_account_requirements_once(self) -> dict[str, str]:
        assert self.page is not None

        body_text = self.page.locator("body").inner_text(timeout=10_000)
        compact = self.compact_text(body_text)
        statuses: dict[str, str] = {}

        for label in REQUIREMENT_LABELS:
            if f"{CHECK_MARK} {label}" in compact or f"{CHECK_MARK}{label}" in compact:
                statuses[label] = "ok"
            elif f"{CROSS_MARK} {label}" in compact or f"{CROSS_MARK}{label}" in compact:
                statuses[label] = "missing"
            else:
                statuses[label] = "unknown"

        return statuses

    def click_open_button(self) -> None:
        assert self.page is not None

        button = self.page.locator(f"xpath={OPEN_BUTTON_XPATH}")
        self.safe_click(button, "boton Open de CS2.free")

    def inspect_open_button_state(self) -> tuple[str | None, bool]:
        assert self.page is not None

        button = self.page.locator(f"xpath={OPEN_BUTTON_XPATH}")
        try:
            button.wait_for(state="visible", timeout=12_000)
        except PlaywrightTimeoutError:
            self.logger.warning("No se encontro el boton Open de CS2.free.")
            return None, False

        try:
            button_text = self.compact_text(button.inner_text(timeout=3_000))
        except Exception:
            button_text = ""

        try:
            button_enabled = button.is_enabled(timeout=3_000)
        except Exception:
            button_enabled = False

        self.logger.info(
            "Boton principal de CS2.free detectado con texto: %s | Habilitado: %s",
            button_text or "sin texto",
            button_enabled,
        )
        return button_text or None, button_enabled

    def is_open_button_cooldown(
        self,
        button_text: str,
        button_enabled: bool,
    ) -> bool:
        compact = self.compact_text(button_text)
        if COOLDOWN_PATTERN.search(compact):
            return True
        if not button_enabled and not OPEN_READY_PATTERN.search(compact):
            return True
        return False

    def handle_post_open_state(self, requirement_statuses: dict[str, str]) -> str:
        assert self.page is not None

        self.logger.info(
            "Esperando el estado posterior a la apertura de la caja de CS2.free."
        )
        self.human_delay(10.0, 13.0)

        body_text = self.page.locator("body").inner_text(timeout=10_000)
        reward_candidates = self.collect_visible_text_candidates()
        reward_text = self.infer_reward_text(reward_candidates, body_text)
        reward_kind = self.infer_reward_kind(reward_text)
        visible_buttons_before_sell = self.collect_visible_button_texts()

        if reward_text:
            self.logger.info(
                "Recompensa candidata detectada en CS2.free: %s | Tipo: %s",
                reward_text,
                reward_kind,
            )
        else:
            self.logger.warning(
                "No se pudo inferir con claridad la recompensa de CS2.free."
            )

        if visible_buttons_before_sell:
            self.logger.info(
                "Botones visibles tras abrir la caja de CS2.free: %s",
                visible_buttons_before_sell,
            )
        else:
            self.logger.warning(
                "No se detectaron botones visibles tras abrir la caja de CS2.free."
            )

        sell_button_text = self.find_sell_button_text()
        sell_clicked = False
        visible_buttons_after_sell = visible_buttons_before_sell

        if reward_kind == "skin" and sell_button_text:
            self.logger.info(
                "Boton de venta detectado tras abrir la caja de CS2.free: %s",
                sell_button_text,
            )
            sell_clicked = self.click_sell_button()
            if sell_clicked:
                self.human_delay(3.0, 5.0)
                visible_buttons_after_sell = self.collect_visible_button_texts()
        elif reward_kind != "skin" and sell_button_text:
            self.logger.info(
                "Se ha detectado un boton de venta, pero la recompensa parece %s. No se intenta vender.",
                reward_kind,
            )

        status = "opened_sold" if sell_clicked else "opened_unsold"
        self.capture_diagnostics(
            status=status,
            requirement_statuses=requirement_statuses,
            reward_text=reward_text,
            reward_kind=reward_kind,
            reward_candidates=reward_candidates,
            visible_buttons_before_sell=visible_buttons_before_sell,
            visible_buttons_after_sell=visible_buttons_after_sell,
            sell_button_text=sell_button_text,
            sell_clicked=sell_clicked,
        )
        return status

    def capture_diagnostics(
        self,
        *,
        status: str,
        requirement_statuses: dict[str, str],
        reward_text: str | None,
        reward_kind: str,
        reward_candidates: list[dict[str, Any]],
        visible_buttons_before_sell: list[str],
        visible_buttons_after_sell: list[str],
        sell_button_text: str | None,
        sell_clicked: bool,
    ) -> None:
        assert self.page is not None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_file = self.diagnostics_dir / f"cs2free_{timestamp}.png"
        text_file = self.diagnostics_dir / f"cs2free_{timestamp}.txt"
        json_file = self.diagnostics_dir / f"cs2free_{timestamp}.json"

        try:
            self.page.screenshot(path=str(screenshot_file), full_page=True)
            body_text = self.page.locator("body").inner_text(timeout=10_000)
            text_file.write_text(body_text, encoding="utf-8")

            snapshot = {
                "captured_at": datetime.now().astimezone().isoformat(),
                "url": self.page.url,
                "status": status,
                "requirement_statuses": requirement_statuses,
                "reward_text": reward_text,
                "reward_kind": reward_kind,
                "reward_candidates": reward_candidates[:25],
                "visible_buttons_before_sell": visible_buttons_before_sell,
                "visible_buttons_after_sell": visible_buttons_after_sell,
                "sell_button_text": sell_button_text,
                "sell_clicked": sell_clicked,
            }
            json_file.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self.logger.info(
                "Diagnostico de CS2.free guardado en %s, %s y %s.",
                screenshot_file,
                text_file,
                json_file,
            )
        except Exception:
            self.logger.exception("No se pudo capturar el diagnostico de CS2.free.")

    def compact_text(self, value: str) -> str:
        return " ".join(value.split()).strip()

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
        return self.safe_click(locator, "boton de vender recompensa en CS2.free", allow_fail=True)

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
                    .slice(0, 50);
                }
                """
            )
        except Exception:
            self.logger.warning("No se pudieron recolectar los botones visibles de CS2.free.")
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
                    document.querySelectorAll("h1, h2, h3, h4, p, span, div, strong, b")
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
                    .slice(0, 240);
                }
                """
            )
        except Exception:
            self.logger.warning("No se pudieron recolectar los textos visibles de CS2.free.")
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

    def infer_reward_text(
        self,
        candidates: list[dict[str, Any]],
        body_text: str | None = None,
    ) -> str | None:
        compact_body = self.compact_text(body_text or "")
        if NO_REWARD_PATTERN.search(compact_body):
            return "Nothing"

        filtered: list[dict[str, Any]] = []
        for candidate in candidates:
            text = str(candidate.get("text", "")).strip()
            if not text:
                continue
            if NO_REWARD_PATTERN.search(text):
                return "Nothing"
            if len(text) > 120:
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

    def infer_reward_kind(self, reward_text: str | None) -> str:
        if not reward_text:
            return "unknown"
        if reward_text.strip().lower() == "nothing":
            return "none"
        if WEAPON_REWARD_PATTERN.search(reward_text):
            return "skin"
        return "unknown"

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
                    "No se pudo hacer hover sobre %s en CS2.free. Se intenta click.",
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

    def human_delay(self, min_seconds: float = 0.8, max_seconds: float = 1.8) -> None:
        time.sleep(random.uniform(min_seconds, max_seconds))

    def prompt_retry(self) -> bool:
        answer = input(
            "El navegador de CS2.free sigue abierto. Pulsa Enter para reintentar o escribe 'q' para salir: "
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
                    self.save_cs2free_session()
                except Exception:
                    self.logger.exception(
                        "Fallo al guardar la sesion de CS2.free durante el cierre."
                    )

            try:
                self.context.close()
            except Exception:
                self.logger.exception("Fallo al cerrar el contexto de CS2.free.")
            self.context = None
            self.page = None

        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                self.logger.exception("Fallo al cerrar Chromium de CS2.free.")
            self.browser = None
