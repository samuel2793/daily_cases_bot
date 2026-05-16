from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from interaction import ask_text

from .keydrop import load_session, save_balance_snapshot
from .steam import SteamAvatarManager

DEFAULT_URL = "https://csgocases.com/es"
DAILY_FREE_CASE_URL_ES = "https://csgocases.com/es/case/caja-gratis-2"
FREE_NICK_CASE_URL_ES = "https://csgocases.com/es/case/caja-gratis"
STEAM_NICK_SUFFIX = "CS2SKINS.GIFT"
CURRENCY_PATTERN = re.compile(r"(?:\$|€)\s*\d+(?:[.,]\d{1,2})?")
CASE_COOLDOWN_PATTERN = re.compile(
    r"(espera\s*\d+\s*[hm]|wait\s*\d+\s*[hm]|available in|disponible en|"
    r"\b\d+\s*h\b|\b\d+\s*min\b)",
    re.IGNORECASE,
)


class ManualFlowAborted(RuntimeError):
    pass


@dataclass(slots=True)
class CSGOCasesSite:
    session_file: Path
    steam_session_file: Path
    steam_avatar_file: Path
    steam_workspace_dir: Path
    balances_file: Path
    logger: logging.Logger
    url: str = DEFAULT_URL

    def run(self) -> str:
        avatar_manager: SteamAvatarManager | None = None
        nickname_manager: SteamAvatarManager | None = None
        completed_manual_cases = 0
        cooldown_cases = 0

        try:
            self.capture_initial_balance_before_cases()

            first_case_status = self.inspect_case_availability(
                case_label="Caja gratis 2 de CSGOCases",
                target_url=DAILY_FREE_CASE_URL_ES,
            )
            if first_case_status != "cooldown":
                avatar_manager = self.apply_steam_avatar_requirement()
                self.wait_for_manual_case_completion(
                    case_label="Caja gratis 2 de CSGOCases",
                    target_url=DAILY_FREE_CASE_URL_ES,
                    requirement_label="avatar de Steam con el logo de CSGOCases",
                )
                self.cleanup_steam_avatar_requirement(avatar_manager)
                avatar_manager = None
                self.capture_balance_after_manual_case(
                    case_label="Caja gratis 2 de CSGOCases",
                    source_url=DAILY_FREE_CASE_URL_ES,
                )
                completed_manual_cases += 1
            else:
                cooldown_cases += 1
                self.logger.info(
                    "Se omite %s porque CSGOCases la muestra en cooldown.",
                    "Caja gratis 2 de CSGOCases",
                )

            second_case_status = self.inspect_case_availability(
                case_label="Caja gratis de CSGOCases",
                target_url=FREE_NICK_CASE_URL_ES,
            )
            if second_case_status != "cooldown":
                nickname_manager = self.apply_steam_profile_name_requirement()
                self.wait_for_manual_case_completion(
                    case_label="Caja gratis de CSGOCases",
                    target_url=FREE_NICK_CASE_URL_ES,
                    requirement_label=f"nick de Steam con '{STEAM_NICK_SUFFIX}'",
                )
                self.cleanup_steam_profile_name_requirement(nickname_manager)
                nickname_manager = None
                self.capture_balance_after_manual_case(
                    case_label="Caja gratis de CSGOCases",
                    source_url=FREE_NICK_CASE_URL_ES,
                )
                completed_manual_cases += 1
            else:
                cooldown_cases += 1
                self.logger.info(
                    "Se omite %s porque CSGOCases la muestra en cooldown.",
                    "Caja gratis de CSGOCases",
                )

            self.logger.info(
                "Flujo manual de CSGOCases finalizado. URLs objetivo: %s | %s",
                DAILY_FREE_CASE_URL_ES,
                FREE_NICK_CASE_URL_ES,
            )
            if cooldown_cases == 2:
                return "cooldown"
            if completed_manual_cases == 2:
                return "manual_completed"
            if completed_manual_cases == 1:
                return "manual_partial"
            return "not_available"
        except ManualFlowAborted as exc:
            self.logger.warning("%s", exc)
            return "manual_cancelled"
        finally:
            self.cleanup_steam_avatar_requirement(avatar_manager)
            self.cleanup_steam_profile_name_requirement(nickname_manager)

    def wait_for_manual_case_completion(
        self,
        case_label: str,
        target_url: str,
        requirement_label: str,
    ) -> None:
        self.logger.warning(
            "%s queda totalmente en modo manual. Ya esta preparado el requisito temporal de Steam: %s.",
            case_label,
            requirement_label,
        )
        self.logger.info("URL manual de %s: %s", case_label, target_url)

        try:
            answer = ask_text(
                f"Abre manualmente '{case_label}' fuera del navegador automatizado.\n"
                f"URL: {target_url}\n\n"
                "Resuelve todo y cuando termines pulsa Enter para continuar. "
                "Escribe 'q' para cancelar y restaurar el cambio temporal de Steam: ",
                title=f"Intervencion manual en {case_label}",
            ).strip().lower()
        except KeyboardInterrupt as exc:
            raise ManualFlowAborted(
                "Flujo manual de CSGOCases cancelado por el usuario."
            ) from exc

        if answer in {"q", "quit", "exit"}:
            raise ManualFlowAborted("Flujo manual de CSGOCases cancelado por el usuario.")

        self.human_delay(1.0, 2.0)

    def apply_steam_avatar_requirement(self) -> SteamAvatarManager:
        if not self.steam_avatar_file.exists():
            raise FileNotFoundError(
                f"No existe la imagen de avatar para Steam en CSGOCases: {self.steam_avatar_file}"
            )

        steam_manager = self.build_steam_manager()
        try:
            steam_manager.start()
            steam_manager.backup_and_apply_from_file(self.steam_avatar_file)
            self.logger.info("Avatar temporal de Steam aplicado para CSGOCases.")
            self.human_delay(2.0, 4.0)
            return steam_manager
        except Exception:
            steam_manager.close()
            raise

    def apply_steam_profile_name_requirement(self) -> SteamAvatarManager:
        steam_manager = self.build_steam_manager()
        try:
            steam_manager.start()
            steam_manager.backup_and_apply_profile_name_prefix_suffix(
                STEAM_NICK_SUFFIX,
                prefix_length=3,
            )
            self.logger.info(
                "Nick temporal corto de Steam aplicado para CSGOCases con sufijo %s.",
                STEAM_NICK_SUFFIX,
            )
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
                    "No se pudo restaurar automaticamente el avatar original de Steam tras CSGOCases."
                )
        except Exception:
            self.logger.exception(
                "Fallo durante la restauracion del avatar de Steam tras CSGOCases."
            )
        finally:
            try:
                steam_manager.close()
            except Exception:
                self.logger.exception("Fallo al cerrar SteamAvatarManager tras CSGOCases.")

    def cleanup_steam_profile_name_requirement(
        self, steam_manager: SteamAvatarManager | None
    ) -> None:
        if steam_manager is None:
            return

        try:
            restored = steam_manager.restore_previous_profile_name()
            if not restored:
                self.logger.warning(
                    "No se pudo restaurar automaticamente el nick original de Steam tras CSGOCases."
                )
        except Exception:
            self.logger.exception(
                "Fallo durante la restauracion del nick de Steam tras CSGOCases."
            )
        finally:
            try:
                steam_manager.close()
            except Exception:
                self.logger.exception("Fallo al cerrar SteamAvatarManager del nick tras CSGOCases.")

    def build_steam_manager(self) -> SteamAvatarManager:
        return SteamAvatarManager(
            session_file=self.steam_session_file,
            workspace_dir=self.steam_workspace_dir,
            logger=logging.getLogger("daily_cases_bot.steam"),
        )

    def capture_initial_balance_before_cases(self) -> None:
        balance_text, balance_value = self.try_read_balance_with_browser(
            target_url=self.url,
            log_context="antes de revisar las cajas de CSGOCases",
        )
        if not balance_text:
            self.logger.warning(
                "No se pudo registrar el saldo inicial de CSGOCases antes de revisar las cajas."
            )
            return

        saved = save_balance_snapshot(
            balances_file=self.balances_file,
            site_name="csgocases",
            balance_text=balance_text,
            balance_value=balance_value,
            source_url=self.url,
            logger=self.logger,
        )
        if saved:
            self.logger.info(
                "Saldo inicial detectado en CSGOCases antes de revisar las cajas: %s",
                balance_text,
            )

    def inspect_case_availability(self, case_label: str, target_url: str) -> str:
        body_text = self.try_read_case_body_text(target_url, case_label)
        if not body_text:
            self.logger.warning(
                "No se pudo determinar si %s esta disponible. Se mantiene el flujo normal.",
                case_label,
            )
            return "unknown"

        if CASE_COOLDOWN_PATTERN.search(body_text):
            self.logger.info(
                "%s aparece en cooldown en CSGOCases. Texto detectado: %s",
                case_label,
                self.extract_case_cooldown_excerpt(body_text),
            )
            return "cooldown"

        self.logger.info("%s parece disponible en CSGOCases.", case_label)
        return "available"

    def capture_balance_after_manual_case(self, case_label: str, source_url: str) -> None:
        previous_balance_text, previous_balance_value = self.get_latest_known_balance()
        current_balance_text, current_balance_value = self.try_read_balance_with_browser(
            target_url=self.url,
            log_context="tras la apertura manual",
        )

        if current_balance_text is None:
            current_balance_text = self.prompt_manual_balance(case_label)
            current_balance_value = (
                self.parse_balance_value(current_balance_text) if current_balance_text else None
            )

        if not current_balance_text:
            self.logger.warning(
                "No se pudo registrar el saldo posterior a %s en CSGOCases.",
                case_label,
            )
            return

        saved = save_balance_snapshot(
            balances_file=self.balances_file,
            site_name="csgocases",
            balance_text=current_balance_text,
            balance_value=current_balance_value,
            source_url=source_url,
            logger=self.logger,
        )
        if not saved:
            self.logger.warning(
                "No se pudo guardar el saldo posterior a %s en CSGOCases.",
                case_label,
            )
            return

        self.log_balance_change(
            case_label=case_label,
            previous_balance_text=previous_balance_text,
            previous_balance_value=previous_balance_value,
            current_balance_text=current_balance_text,
            current_balance_value=current_balance_value,
        )

    def get_latest_known_balance(self) -> tuple[str | None, float | None]:
        if not self.balances_file.exists():
            return None, None

        try:
            store = json.loads(self.balances_file.read_text(encoding="utf-8"))
        except Exception:
            self.logger.warning(
                "No se pudo leer el historial de saldos para CSGOCases desde %s.",
                self.balances_file,
            )
            return None, None

        latest = store.get("csgocases", {}).get("latest")
        if not latest:
            return None, None
        return latest.get("balance_text"), latest.get("balance_value")

    def try_read_balance_with_browser(
        self,
        *,
        target_url: str,
        log_context: str,
    ) -> tuple[str | None, float | None]:
        session_data = load_session(self.session_file, self.logger)
        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None

        try:
            with sync_playwright() as playwright:
                browser, context, page = self.open_balance_check_browser(
                    playwright,
                    session_data,
                )
                self.logger.info(
                    "Revisando automaticamente el saldo de CSGOCases %s.",
                    log_context,
                )
                page.goto(target_url, wait_until="domcontentloaded", timeout=25_000)
                self.wait_for_light_page_ready(page)
                balance_text = self.find_balance_text(page)
                if not balance_text:
                    self.logger.warning(
                        "No se pudo localizar automaticamente el saldo de CSGOCases %s.",
                        log_context,
                    )
                    return None, None

                self.logger.info(
                    "Saldo detectado automaticamente en CSGOCases %s: %s",
                    log_context,
                    balance_text,
                )
                return balance_text, self.parse_balance_value(balance_text)
        except Exception:
            self.logger.warning(
                "La revision automatica del saldo de CSGOCases fallo %s.",
                log_context,
                exc_info=True,
            )
            return None, None
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    def try_read_case_body_text(self, target_url: str, case_label: str) -> str | None:
        session_data = load_session(self.session_file, self.logger)
        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None

        try:
            with sync_playwright() as playwright:
                browser, context, page = self.open_balance_check_browser(
                    playwright,
                    session_data,
                )
                self.logger.info("Comprobando disponibilidad de %s.", case_label)
                page.goto(target_url, wait_until="domcontentloaded", timeout=25_000)
                self.wait_for_light_page_ready(page)
                return self.compact_text(page.locator("body").inner_text(timeout=10_000))
        except Exception:
            self.logger.warning(
                "La comprobacion de disponibilidad de %s fallo en CSGOCases.",
                case_label,
                exc_info=True,
            )
            return None
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    def extract_case_cooldown_excerpt(self, body_text: str) -> str:
        match = CASE_COOLDOWN_PATTERN.search(body_text)
        if not match:
            return "sin texto de cooldown"
        return match.group(0)

    def open_balance_check_browser(
        self,
        playwright: Playwright,
        session_data: dict | None,
    ) -> tuple[Browser, BrowserContext, Page]:
        browser = playwright.chromium.launch(
            headless=False,
            slow_mo=60,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )

        context_kwargs = {
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

        context = browser.new_context(**context_kwargs)
        context.set_default_timeout(12_000)
        context.set_default_navigation_timeout(25_000)
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['es-ES', 'es', 'en-US', 'en']
            });
            """
        )
        page = context.new_page()
        page.bring_to_front()
        return browser, context, page

    def wait_for_light_page_ready(self, page: Page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "CSGOCases no entro en domcontentloaded durante la revision de saldo. Se continua."
            )
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            self.logger.info(
                "CSGOCases no entro en networkidle durante la revision de saldo. Se continua."
            )

    def find_balance_text(self, page: Page) -> str | None:
        try:
            balance_text = page.evaluate(
                """
                (patternSource) => {
                  const pattern = new RegExp(patternSource);
                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style &&
                      style.visibility !== 'hidden' &&
                      style.display !== 'none' &&
                      rect.width > 0 &&
                      rect.height > 0;
                  };

                  const roots = Array.from(
                    document.querySelectorAll("header, [class*='header'], [id*='header']")
                  );
                  const candidates = [];

                  for (const root of roots) {
                    const nodes = [root, ...root.querySelectorAll("*")];
                    for (const node of nodes) {
                      if (!isVisible(node)) continue;
                      const text = (node.innerText || node.textContent || "").trim();
                      if (!text) continue;
                      const match = text.match(pattern);
                      if (!match) continue;
                      const rect = node.getBoundingClientRect();
                      candidates.push({ text: match[0], y: rect.y, x: rect.x });
                    }
                  }

                  candidates.sort((a, b) => (a.y - b.y) || (a.x - b.x));
                  return candidates.length ? candidates[0].text : null;
                }
                """,
                CURRENCY_PATTERN.pattern,
            )
        except Exception:
            return None

        if not balance_text:
            return None
        return self.compact_text(str(balance_text))

    def prompt_manual_balance(self, case_label: str) -> str | None:
        answer = ask_text(
            f"No se pudo leer automaticamente el saldo de CSGOCases tras '{case_label}'. "
            "Si quieres registrarlo, pega aqui el saldo actual mostrado en la web (por ejemplo $1.19) o pulsa Enter para omitir: ",
            title="Saldo manual de CSGOCases",
        ).strip()
        return answer or None

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
                "No se pudo convertir el saldo de CSGOCases '%s' a numero.",
                balance_text,
            )
            return None

    def log_balance_change(
        self,
        *,
        case_label: str,
        previous_balance_text: str | None,
        previous_balance_value: float | None,
        current_balance_text: str,
        current_balance_value: float | None,
    ) -> None:
        if previous_balance_value is None or current_balance_value is None:
            self.logger.info(
                "Saldo registrado tras %s en CSGOCases: %s",
                case_label,
                current_balance_text,
            )
            return

        delta = round(current_balance_value - previous_balance_value, 2)
        if delta == 0:
            self.logger.info(
                "Saldo de CSGOCases sin cambios tras %s: %s",
                case_label,
                current_balance_text,
            )
            return

        self.logger.info(
            "Cambio de saldo detectado en CSGOCases tras %s: %s -> %s (delta %s)",
            case_label,
            previous_balance_text or "sin saldo previo",
            current_balance_text,
            delta,
        )

    def compact_text(self, value: str) -> str:
        return " ".join(value.split()).strip()

    def human_delay(self, min_seconds: float = 0.8, max_seconds: float = 1.8) -> None:
        time.sleep(random.uniform(min_seconds, max_seconds))
