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
CLAIM_PATTERN = re.compile(r"\b(claim|reclamar)\b", re.IGNORECASE)
SELL_PATTERN = re.compile(r"(vender|sell)", re.IGNORECASE)
RAFFLE_BUTTON_PATTERN = re.compile(
    r"(participar en sorteos|participar en sorteo|join giveaways|giveaways?)",
    re.IGNORECASE,
)
WINNING_MODAL_PATTERN = re.compile(
    r"(congratulations!\s*your winning|your winning:|tu premio|has ganado)",
    re.IGNORECASE,
)
RAFFLE_REWARD_LABEL_PATTERN = re.compile(
    r"(participaci[oó]n en sorteos|participar en sorteos|sorteo)",
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
    r"(bloodycase|daily free|claim|sell|vender|free|daily|reward|recompensa|"
    r"saldo|balance|steam|avatar|nick|profile|abrir|open|case|caja|upgrade|"
    r"battle|contracts|upgrade|withdraw|retirar|login|sign in)",
    re.IGNORECASE,
)
PRE_CLAIM_BODY_PATTERN = re.compile(
    r"(ready\s*[·-]?\s*click your case above to claim|"
    r"complete all requirements to claim your daily free bonus|"
    r"gratis cs2 skins|how it works)",
    re.IGNORECASE,
)
POST_CLAIM_MAX_ATTEMPTS = 14
POST_CLAIM_REWARD_STABLE_ATTEMPTS = 2


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
                                balance_text_before=balance_text,
                                balance_value_before=balance_value,
                                balance_text_after=None,
                                balance_value_after=None,
                                claim_button_text=None,
                                status="button_not_found",
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons_before_sell=[],
                                visible_buttons_after_sell=[],
                                sell_button_text=None,
                                sell_offer_value=None,
                                sell_clicked=False,
                                gain_value=None,
                            )
                            self.logger.info(
                                "BloodyCase no mostro boton CLAIM tras cargar la daily free. Se interpreta como cooldown/no disponible. Saldo detectado: %s",
                                balance_text,
                            )
                            return "cooldown"

                        if not CLAIM_PATTERN.search(button_text):
                            self.capture_post_claim_diagnostics(
                                balance_text_before=balance_text,
                                balance_value_before=balance_value,
                                balance_text_after=None,
                                balance_value_after=None,
                                claim_button_text=button_text,
                                status="not_claimable",
                                reward_text=None,
                                reward_kind="unknown",
                                reward_candidates=[],
                                visible_buttons_before_sell=[],
                                visible_buttons_after_sell=[],
                                sell_button_text=None,
                                sell_offer_value=None,
                                sell_clicked=False,
                                gain_value=None,
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
                        post_claim_status = self.handle_post_claim_state(
                            balance_text_before=balance_text,
                            balance_value_before=balance_value,
                            claim_button_text=button_text,
                        )
                        self.logger.info(
                            "Flujo de BloodyCase finalizado. Saldo detectado: %s | Boton inicial: %s | Estado postapertura: %s",
                            balance_text,
                            button_text,
                            post_claim_status,
                        )
                        return post_claim_status
                    except KeyboardInterrupt:
                        self.abort_pending_page_activity()
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

        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de BloodyCase no entro en domcontentloaded. Se continua con timeout controlado."
            )
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "La pagina de BloodyCase no entro en networkidle. Se continua con timeout controlado."
            )

        self.page.locator("body").wait_for(state="visible", timeout=10_000)

    def abort_pending_page_activity(self) -> None:
        if self.page is None:
            return

        try:
            self.page.evaluate("window.stop()")
        except Exception:
            pass

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        if self.is_logged_in():
            self.logger.info("Sesion reutilizada automaticamente en BloodyCase.")
            return

        self.logger.warning("No hay sesion valida de BloodyCase. Se requiere login manual.")

        while True:
            ask_text(
                "Haz login manual en BloodyCase en la ventana de Chromium y luego pulsa Enter aqui. ",
                title="Login manual en BloodyCase",
            )
            self.human_delay(1.5, 2.8)
            self.page.goto(self.url, wait_until="domcontentloaded")
            self.wait_for_page_ready()

            if self.is_logged_in():
                save_session(self.context, self.session_file, self.logger)
                self.logger.info("Login manual detectado en BloodyCase y sesion guardada.")
                return

            answer = ask_text(
                "No se pudo confirmar el login en BloodyCase. Enter para revisar otra vez o 'q' para salir: ",
                title="Reintento de login en BloodyCase",
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
        *,
        balance_text_before: str,
        balance_value_before: float | None,
        balance_text_after: str | None,
        balance_value_after: float | None,
        claim_button_text: str | None,
        status: str,
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
                "balance_text_before": balance_text_before,
                "balance_value_before": balance_value_before,
                "balance_text_after": balance_text_after,
                "balance_value_after": balance_value_after,
                "claim_button_text": claim_button_text,
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

    def handle_post_claim_state(
        self,
        balance_text_before: str,
        balance_value_before: float | None,
        claim_button_text: str | None,
    ) -> str:
        assert self.page is not None

        self.logger.info(
            "Esperando el estado posterior a la apertura de la caja de BloodyCase. "
            "Se registraran los cambios intermedios hasta confirmar recompensa y opciones de venta."
        )
        observations: list[dict[str, Any]] = []
        reward_candidates: list[dict[str, Any]] = []
        reward_text: str | None = None
        reward_kind = "unknown"
        visible_buttons_before_sell: list[str] = []
        sell_button_text_probe: str | None = None
        body_text: str | None = None
        reward_stable_hits = 0
        last_signature: tuple[str | None, str, str | None] | None = None

        for attempt in range(1, POST_CLAIM_MAX_ATTEMPTS + 1):
            if attempt == 1:
                self.human_delay(4.0, 5.5)
            else:
                self.human_delay(2.0, 3.5)
            reward_candidates = self.collect_visible_text_candidates()
            visible_buttons_before_sell = self.collect_visible_button_texts()
            body_text = self.try_read_body_text()
            reward_text = self.infer_reward_text(
                reward_candidates,
                visible_buttons_before_sell,
                body_text=body_text,
            )
            reward_kind = self.infer_reward_kind(
                reward_text,
                visible_buttons_before_sell,
                body_text=body_text,
            )
            if reward_kind == "raffle":
                reward_text = "Participacion en sorteos"
            sell_button_text_probe = self.find_sell_button_text()
            observation = {
                "attempt": attempt,
                "captured_at": datetime.now().astimezone().isoformat(),
                "reward_text": reward_text,
                "reward_kind": reward_kind,
                "sell_button_text": sell_button_text_probe,
                "visible_buttons": visible_buttons_before_sell,
            }
            observations.append(observation)

            signature = (reward_text, reward_kind, sell_button_text_probe)
            if signature != last_signature:
                self.logger.info(
                    "Estado postapertura de BloodyCase (%s/%s) | Recompensa: %s | Tipo: %s | Vender: %s",
                    attempt,
                    POST_CLAIM_MAX_ATTEMPTS,
                    reward_text or "sin detectar",
                    reward_kind,
                    sell_button_text_probe or "no",
                )
                last_signature = signature

            if reward_text:
                reward_stable_hits += 1
            else:
                reward_stable_hits = 0

            if reward_kind == "skin" and sell_button_text_probe and reward_stable_hits >= POST_CLAIM_REWARD_STABLE_ATTEMPTS:
                break
            if reward_kind == "raffle" and reward_stable_hits >= POST_CLAIM_REWARD_STABLE_ATTEMPTS:
                break
            if reward_text and reward_kind == "unknown" and reward_stable_hits >= POST_CLAIM_REWARD_STABLE_ATTEMPTS:
                break
            if sell_button_text_probe and reward_kind != "skin":
                self.logger.info(
                    "BloodyCase ya muestra boton de venta, pero la recompensa aun no es una skin clara. Se sigue esperando."
                )
            elif body_text and PRE_CLAIM_BODY_PATTERN.search(body_text):
                self.logger.info(
                    "BloodyCase sigue mostrando una vista previa/no concluyente tras el CLAIM. Se espera un poco mas antes de registrar la salida."
                )

        if reward_text:
            self.logger.info(
                "Recompensa candidata detectada en BloodyCase: %s | Tipo: %s",
                reward_text,
                reward_kind,
            )
        else:
            self.logger.warning(
                "No se pudo inferir con claridad la recompensa de BloodyCase."
            )

        if visible_buttons_before_sell:
            self.logger.info(
                "Botones visibles tras abrir la caja de BloodyCase: %s",
                visible_buttons_before_sell,
            )
        else:
            self.logger.warning(
                "No se detectaron botones visibles tras abrir la caja de BloodyCase."
            )

        sell_button_text = self.find_sell_button_text()
        sell_offer_value = self.parse_balance_value(sell_button_text) if sell_button_text else None
        sell_clicked = False
        balance_text_after = None
        balance_value_after = None
        gain_value = None
        visible_buttons_after_sell = visible_buttons_before_sell

        if reward_kind == "skin" and sell_button_text:
            self.logger.info(
                "Boton de venta detectado tras abrir la caja de BloodyCase: %s",
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
                        "Ganancia estimada por la venta en BloodyCase: %s",
                        gain_value,
                    )
                visible_buttons_after_sell = self.collect_visible_button_texts()
        elif reward_kind != "skin" and sell_button_text:
            self.logger.info(
                "Se ha detectado un boton de venta, pero la recompensa parece %s. No se intenta vender.",
                reward_kind,
            )

        if not reward_text:
            status = "claim_unresolved"
        elif reward_kind == "raffle":
            status = "claim_raffle"
        else:
            status = "claim_sold" if sell_clicked else "claim_unsold"
        self.capture_post_claim_diagnostics(
            balance_text_before=balance_text_before,
            balance_value_before=balance_value_before,
            balance_text_after=balance_text_after,
            balance_value_after=balance_value_after,
            claim_button_text=claim_button_text,
            status=status,
            reward_text=reward_text,
            reward_kind=reward_kind,
            reward_candidates=reward_candidates,
            visible_buttons_before_sell=visible_buttons_before_sell,
            visible_buttons_after_sell=visible_buttons_after_sell,
            sell_button_text=sell_button_text,
            sell_offer_value=sell_offer_value,
            sell_clicked=sell_clicked,
            gain_value=gain_value,
            observations=observations,
        )
        return status

    def try_read_body_text(self) -> str | None:
        assert self.page is not None

        try:
            body_text = self.page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return None
        return self.compact_text(body_text)

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

    def try_read_balance_text(self) -> str | None:
        try:
            return self.read_balance_text()
        except Exception:
            self.logger.warning("No se pudo releer el saldo de BloodyCase tras la apertura.")
            return None

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
        return self.safe_click(locator, "boton de vender recompensa en BloodyCase", allow_fail=True)

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
            self.logger.warning("No se pudieron recolectar los botones visibles de BloodyCase.")
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
                    .slice(0, 200);
                }
                """
            )
        except Exception:
            self.logger.warning("No se pudieron recolectar los textos visibles de BloodyCase.")
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
        visible_buttons: list[str] | None = None,
        body_text: str | None = None,
    ) -> str | None:
        raffle_reward = self.infer_raffle_reward_text(
            candidates,
            visible_buttons or [],
            body_text=body_text,
        )
        if raffle_reward:
            return raffle_reward

        filtered: list[dict[str, Any]] = []
        for candidate in candidates:
            text = str(candidate.get("text", "")).strip()
            if not text:
                continue
            if len(text) > 100:
                continue
            if GENERIC_UI_TEXT_PATTERN.search(text):
                continue
            if self.is_generic_reward_text(text):
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

    def infer_raffle_reward_text(
        self,
        candidates: list[dict[str, Any]],
        visible_buttons: list[str],
        *,
        body_text: str | None = None,
    ) -> str | None:
        if body_text and WINNING_MODAL_PATTERN.search(body_text) and (
            RAFFLE_BUTTON_PATTERN.search(body_text)
            or RAFFLE_REWARD_LABEL_PATTERN.search(body_text)
        ):
            return "Participacion en sorteos"

        raffle_button_texts = [
            text for text in visible_buttons if RAFFLE_BUTTON_PATTERN.search(text)
        ]
        if not raffle_button_texts:
            return None

        if body_text and WINNING_MODAL_PATTERN.search(body_text):
            return "Participacion en sorteos"

        raffle_button = raffle_button_texts[0]
        button_candidates = [
            candidate
            for candidate in candidates
            if self.compact_text(str(candidate.get("text", ""))) == self.compact_text(raffle_button)
        ]
        if not button_candidates:
            return None

        button_candidate = button_candidates[0]
        button_x = float(button_candidate.get("x", 0) or 0)
        button_y = float(button_candidate.get("y", 0) or 0)
        button_width = float(button_candidate.get("width", 0) or 0)
        button_center_x = button_x + (button_width / 2)

        reward_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            text = str(candidate.get("text", "")).strip()
            if not text:
                continue
            if len(text) > 80:
                continue
            if self.is_generic_reward_text(text):
                continue
            if GENERIC_UI_TEXT_PATTERN.search(text):
                continue
            if RAFFLE_BUTTON_PATTERN.search(text):
                continue
            if re.fullmatch(r"[\d\s.,:$€£-]+", text):
                continue

            candidate_x = float(candidate.get("x", 0) or 0)
            candidate_y = float(candidate.get("y", 0) or 0)
            candidate_width = float(candidate.get("width", 0) or 0)
            candidate_center_x = candidate_x + (candidate_width / 2)

            if candidate_y >= button_y:
                continue
            if button_y - candidate_y > 420:
                continue
            if abs(candidate_center_x - button_center_x) > 240:
                continue

            reward_candidates.append(candidate)

        if not reward_candidates:
            return None

        preferred = [
            candidate
            for candidate in reward_candidates
            if "|" in str(candidate.get("text", ""))
        ]
        if preferred:
            reward_candidates = preferred

        reward_candidates.sort(
            key=lambda item: (
                abs(
                    (
                        float(item.get("x", 0) or 0)
                        + (float(item.get("width", 0) or 0) / 2)
                    )
                    - button_center_x
                ),
                abs(button_y - float(item.get("y", 0) or 0)),
                -float(item.get("fontSize", 0) or 0),
            )
        )
        return str(reward_candidates[0].get("text", "")).strip() or None

    def infer_reward_kind(
        self,
        reward_text: str | None,
        visible_buttons: list[str] | None = None,
        body_text: str | None = None,
    ) -> str:
        if not reward_text:
            return "unknown"
        if reward_text and RAFFLE_REWARD_LABEL_PATTERN.search(reward_text):
            return "raffle"
        if body_text and WINNING_MODAL_PATTERN.search(body_text):
            return "raffle"
        if visible_buttons and any(
            RAFFLE_BUTTON_PATTERN.search(text) for text in visible_buttons
        ):
            return "raffle"
        if WEAPON_REWARD_PATTERN.search(reward_text):
            return "skin"
        return "unknown"

    def is_generic_reward_text(self, text: str) -> bool:
        compact = self.compact_text(text)
        return bool(PRE_CLAIM_BODY_PATTERN.search(compact))

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
        answer = ask_text(
            "El navegador de BloodyCase sigue abierto. Pulsa Enter para reintentar o escribe 'q' para salir: ",
            title="Reintentar BloodyCase",
        ).strip().lower()
        return answer not in {"q", "quit", "exit"}

    def close(self) -> None:
        if self.context is not None:
            self.abort_pending_page_activity()
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
