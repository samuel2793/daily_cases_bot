from __future__ import annotations

import json
import logging
import mimetypes
import random
import re
import time
import urllib.request
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

STEAM_PROFILE_URL = "https://steamcommunity.com/my"
STEAM_EDIT_PROFILE_URL = "https://steamcommunity.com/my/edit"
STEAM_EDIT_AVATAR_URL = "https://steamcommunity.com/my/edit/avatar"
STEAM_LOGIN_PATTERN = re.compile(r"(sign in|iniciar sesi[oó]n|login)", re.IGNORECASE)
STEAM_SAVE_PATTERN = re.compile(r"(save|guardar)", re.IGNORECASE)


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


@dataclass(slots=True)
class SteamAvatarManager:
    session_file: Path
    workspace_dir: Path
    logger: logging.Logger
    headless: bool = False
    slow_mo_ms: int = 90
    browser: Browser | None = field(default=None, init=False)
    context: BrowserContext | None = field(default=None, init=False)
    page: Page | None = field(default=None, init=False)
    playwright_manager: Any = field(default=None, init=False)
    owns_playwright_manager: bool = field(default=False, init=False)
    backup_dir: Path = field(init=False)
    backup_file: Path = field(init=False)
    metadata_file: Path = field(init=False)
    profile_dir: Path = field(init=False)
    profile_metadata_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.backup_dir = self.workspace_dir / "steam_avatar"
        self.backup_file = self.backup_dir / "original_avatar"
        self.metadata_file = self.backup_dir / "metadata.json"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = self.workspace_dir / "steam_profile"
        self.profile_metadata_file = self.profile_dir / "metadata.json"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def start(self, playwright: Playwright | None = None) -> None:
        playwright_manager = playwright
        owns_playwright_manager = False
        if playwright_manager is None:
            playwright_manager = sync_playwright().start()
            owns_playwright_manager = True

        try:
            self._open_browser(playwright_manager)
            self.ensure_authenticated()
        except Exception:
            if owns_playwright_manager:
                playwright_manager.stop()
            raise
        self.playwright_manager = playwright_manager
        self.owns_playwright_manager = owns_playwright_manager

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
                    self.logger.exception("Fallo al guardar la sesion de Steam durante el cierre.")

            try:
                self.context.close()
            except Exception:
                self.logger.exception("Fallo al cerrar el contexto de Steam.")
            self.context = None
            self.page = None

        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                self.logger.exception("Fallo al cerrar Chromium de Steam.")
            self.browser = None

        playwright_manager = self.playwright_manager
        if playwright_manager is not None and self.owns_playwright_manager:
            playwright_manager.stop()
        self.playwright_manager = None
        self.owns_playwright_manager = False

    def backup_and_apply_from_url(self, image_url: str) -> Path:
        original_avatar = self.backup_current_avatar()
        temporary_avatar = self.download_remote_image(image_url, self.backup_dir / "temporary_avatar")
        self.apply_avatar_from_file(temporary_avatar)
        self.logger.info(
            "Avatar temporal aplicado en Steam. Backup original: %s | Temporal: %s",
            original_avatar,
            temporary_avatar,
        )
        return temporary_avatar

    def backup_and_apply_from_file(self, image_path: Path) -> Path:
        original_avatar = self.backup_current_avatar()
        self.apply_avatar_from_file(image_path)
        self.logger.info(
            "Avatar temporal aplicado desde archivo local. Backup original: %s | Temporal: %s",
            original_avatar,
            image_path,
        )
        return image_path

    def restore_previous_avatar(self) -> bool:
        if not self.metadata_file.exists():
            self.logger.warning("No hay metadata de backup para restaurar el avatar de Steam.")
            return False

        try:
            metadata = json.loads(self.metadata_file.read_text(encoding="utf-8"))
        except Exception:
            self.logger.exception("No se pudo leer la metadata de backup de Steam.")
            return False

        backup_path = Path(metadata["backup_file"])
        if not backup_path.exists():
            self.logger.warning("El archivo de backup ya no existe: %s", backup_path)
            return False

        self.apply_avatar_from_file(backup_path)
        self.logger.info("Avatar original de Steam restaurado desde %s.", backup_path)
        return True

    def backup_and_apply_profile_name_suffix(self, suffix: str) -> str:
        current_name = self.backup_current_profile_name()
        updated_name = self.build_profile_name_with_suffix(current_name, suffix)
        self.apply_profile_name(updated_name)
        self.logger.info(
            "Nick temporal aplicado en Steam. Original: %s | Temporal: %s",
            current_name,
            updated_name,
        )
        return updated_name

    def backup_and_apply_profile_name_prefix_suffix(
        self,
        suffix: str,
        prefix_length: int = 3,
    ) -> str:
        current_name = self.backup_current_profile_name()
        updated_name = self.build_profile_name_with_prefix_suffix(
            current_name,
            suffix,
            prefix_length=prefix_length,
        )
        self.apply_profile_name(updated_name)
        self.logger.info(
            "Nick temporal corto aplicado en Steam. Original: %s | Temporal: %s",
            current_name,
            updated_name,
        )
        return updated_name

    def restore_previous_profile_name(self) -> bool:
        if not self.profile_metadata_file.exists():
            self.logger.warning("No hay metadata de backup para restaurar el nick de Steam.")
            return False

        try:
            metadata = json.loads(self.profile_metadata_file.read_text(encoding="utf-8"))
        except Exception:
            self.logger.exception("No se pudo leer la metadata de backup del nick de Steam.")
            return False

        original_name = str(metadata.get("original_profile_name", "")).strip()
        if not original_name:
            self.logger.warning("La metadata de backup del nick de Steam no contiene un valor valido.")
            return False

        self.apply_profile_name(original_name)
        self.logger.info("Nick original de Steam restaurado: %s", original_name)
        return True

    def backup_current_profile_name(self) -> str:
        current_name = self.get_current_profile_name()
        self.profile_metadata_file.write_text(
            json.dumps(
                {
                    "original_profile_name": current_name,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.logger.info("Nick actual de Steam guardado: %s", current_name)
        return current_name

    def get_current_profile_name(self) -> str:
        assert self.page is not None

        self.page.goto(STEAM_EDIT_PROFILE_URL, wait_until="domcontentloaded")
        self.wait_for_page_ready()

        profile_name_input = self.find_profile_name_input()
        if profile_name_input is None:
            raise RuntimeError("No se encontro el campo del nick de Steam.")

        current_name = (profile_name_input.input_value(timeout=5_000) or "").strip()
        if not current_name:
            raise RuntimeError("El campo del nick de Steam esta vacio.")

        self.logger.info("Nick actual de Steam detectado: %s", current_name)
        return current_name

    def build_profile_name_with_suffix(self, current_name: str, suffix: str) -> str:
        normalized_current_name = " ".join(current_name.split()).strip()
        normalized_suffix = " ".join(suffix.split()).strip()
        if not normalized_suffix:
            raise ValueError("El sufijo del nick de Steam no puede estar vacio.")

        if normalized_suffix.casefold() in normalized_current_name.casefold():
            return normalized_current_name

        if normalized_current_name:
            return f"{normalized_current_name} {normalized_suffix}"
        return normalized_suffix

    def build_profile_name_with_prefix_suffix(
        self,
        current_name: str,
        suffix: str,
        prefix_length: int = 3,
    ) -> str:
        normalized_suffix = " ".join(suffix.split()).strip()
        if not normalized_suffix:
            raise ValueError("El sufijo del nick de Steam no puede estar vacio.")
        if prefix_length <= 0:
            raise ValueError("El numero de caracteres del prefijo debe ser mayor que cero.")

        prefix = self.extract_profile_name_prefix(current_name, prefix_length)
        if not prefix:
            raise RuntimeError("No se pudo derivar un prefijo corto valido del nick actual de Steam.")

        return f"{prefix} {normalized_suffix}"

    def extract_profile_name_prefix(self, current_name: str, prefix_length: int) -> str:
        compact_name = " ".join(current_name.split()).strip()
        if not compact_name:
            return ""

        useful_chars: list[str] = []
        for char in compact_name:
            category = unicodedata.category(char)
            if category.startswith(("L", "N")):
                useful_chars.append(char)
            if len(useful_chars) >= prefix_length:
                break

        if len(useful_chars) >= prefix_length:
            return "".join(useful_chars)

        fallback_chars = [char for char in compact_name if not char.isspace()]
        return "".join(fallback_chars[:prefix_length])

    def apply_profile_name(self, profile_name: str) -> None:
        assert self.page is not None

        normalized_name = " ".join(profile_name.split()).strip()
        if not normalized_name:
            raise ValueError("El nick de Steam no puede estar vacio.")

        self.page.goto(STEAM_EDIT_PROFILE_URL, wait_until="domcontentloaded")
        self.wait_for_page_ready()

        profile_name_input = self.find_profile_name_input()
        if profile_name_input is None:
            raise RuntimeError("No se encontro el campo del nick de Steam.")

        profile_name_input.click(timeout=5_000)
        self.human_delay(0.2, 0.6)
        profile_name_input.fill(normalized_name, timeout=10_000)
        self.human_delay(0.8, 1.5)

        save_button = self.first_visible(
            [
                self.page.get_by_role("button", name=STEAM_SAVE_PATTERN).first,
                self.page.locator("input[type='submit'][value*='Save']").first,
                self.page.locator("input[type='submit'][value*='Guardar']").first,
            ],
            timeout_ms=10_000,
        )
        if save_button is None:
            raise RuntimeError("No se encontro el boton para guardar el nick en Steam.")

        self.safe_click(save_button, "guardar nick en Steam")
        self.human_delay(2.0, 3.5)

    def backup_current_avatar(self) -> Path:
        profile_avatar_url = self.get_current_avatar_url()
        suffix = self.guess_suffix(profile_avatar_url, default=".jpg")
        backup_path = self.backup_file.with_suffix(suffix)

        self.download_remote_image(profile_avatar_url, backup_path)
        self.metadata_file.write_text(
            json.dumps(
                {
                    "backup_file": str(backup_path),
                    "original_avatar_url": profile_avatar_url,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.logger.info("Avatar actual de Steam guardado en %s.", backup_path)
        return backup_path

    def get_current_avatar_url(self) -> str:
        assert self.page is not None

        self.page.goto(STEAM_PROFILE_URL, wait_until="domcontentloaded")
        self.wait_for_page_ready()

        avatar_url = self.find_best_avatar_url()
        if not avatar_url:
            raise RuntimeError("No se pudo localizar la URL del avatar real de Steam.")

        self.logger.info("Avatar actual de Steam detectado: %s", avatar_url)
        return avatar_url

    def find_best_avatar_url(self) -> str | None:
        assert self.page is not None

        candidate_entries = self.page.locator(
            ".profile_avatar_frame img, .playerAvatarAutoSizeInner img, img"
        ).evaluate_all(
            """
            elements => elements
                .filter(el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                })
                .map(el => ({
                    src: el.getAttribute('src') || '',
                    currentSrc: el.currentSrc || '',
                    className: el.className || '',
                    alt: el.getAttribute('alt') || ''
                }))
            """
        )

        normalized_sources: list[str] = []
        for entry in candidate_entries:
            if not isinstance(entry, dict):
                continue

            candidate_values = [entry.get("currentSrc", ""), entry.get("src", "")]
            class_name = str(entry.get("className", "")).lower()
            alt_text = str(entry.get("alt", "")).lower()

            for source in candidate_values:
                if not isinstance(source, str):
                    continue
                source = source.strip()
                if not source:
                    continue
                if not self.looks_like_avatar_url(source):
                    continue
                if self.looks_like_frame_asset(source, class_name, alt_text):
                    continue
                normalized_sources.append(source)

        if not normalized_sources:
            return None

        def avatar_score(source: str) -> tuple[int, int]:
            full_score = 1 if "_full." in source else 0
            medium_penalty = -1 if "_medium." in source else 0
            return (full_score + medium_penalty, len(source))

        normalized_sources.sort(key=avatar_score, reverse=True)
        self.logger.debug("Candidatos de avatar Steam detectados: %s", normalized_sources)
        return normalized_sources[0]

    def looks_like_avatar_url(self, source: str) -> bool:
        parsed = urlparse(source)
        host = parsed.netloc.lower()
        path = parsed.path.lower()

        if "steamstatic.com" not in host:
            return False
        if "avatar" not in host and "/avatars/" not in path and "avatar" not in path:
            return False
        if not path.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return False
        return True

    def looks_like_frame_asset(self, source: str, class_name: str, alt_text: str) -> bool:
        haystack = f"{source} {class_name} {alt_text}".lower()
        frame_terms = ("frame", "border", "overlay", "decoration")
        return any(term in haystack for term in frame_terms)

    def apply_avatar_from_file(self, image_path: Path) -> None:
        assert self.page is not None

        if not image_path.exists():
            raise FileNotFoundError(f"No existe la imagen de avatar: {image_path}")

        self.page.goto(STEAM_EDIT_AVATAR_URL, wait_until="domcontentloaded")
        self.wait_for_page_ready()

        file_input = self.page.locator("input[type='file']").first
        file_input.wait_for(state="attached", timeout=15_000)
        file_input.set_input_files(str(image_path))
        self.human_delay(1.0, 2.0)

        save_button = self.first_visible(
            [
                self.page.get_by_role("button", name=STEAM_SAVE_PATTERN).first,
                self.page.locator("input[type='submit'][value*='Save']").first,
                self.page.locator("input[type='submit'][value*='Guardar']").first,
            ],
            timeout_ms=10_000,
        )
        if save_button is None:
            raise RuntimeError("No se encontro el boton para guardar el avatar en Steam.")

        self.safe_click(save_button, "guardar avatar en Steam")
        self.human_delay(2.0, 3.5)

    def find_profile_name_input(self) -> Locator | None:
        assert self.page is not None

        return self.first_visible(
            [
                self.page.locator("input[name='personaName']").first,
                self.page.locator("#personaName").first,
                self.page.locator("input[name='profileName']").first,
            ],
            timeout_ms=10_000,
        )

    def ensure_authenticated(self) -> None:
        assert self.page is not None
        assert self.context is not None

        self.page.goto(STEAM_EDIT_AVATAR_URL, wait_until="domcontentloaded")
        self.wait_for_page_ready()

        if self.is_logged_in():
            self.logger.info("Sesion de Steam reutilizada automaticamente.")
            return

        self.logger.warning("No hay sesion valida de Steam. Se requiere login manual.")

        while True:
            input(
                "Haz login manual en Steam en la ventana de Chromium y luego pulsa Enter aqui. "
            )
            self.human_delay(1.5, 2.8)
            self.page.goto(STEAM_EDIT_AVATAR_URL, wait_until="domcontentloaded")
            self.wait_for_page_ready()

            if self.is_logged_in():
                save_session(self.context, self.session_file, self.logger)
                self.logger.info("Login manual de Steam detectado y sesion guardada.")
                return

            answer = input(
                "No se pudo confirmar el login de Steam. Enter para revisar otra vez o 'q' para salir: "
            ).strip().lower()
            if answer in {"q", "quit", "exit"}:
                raise RuntimeError("Login manual de Steam cancelado por el usuario.")

    def is_logged_in(self) -> bool:
        assert self.page is not None

        if "login/home" in self.page.url:
            return False

        guest_locators = [
            self.page.get_by_role("link", name=STEAM_LOGIN_PATTERN).first,
            self.page.get_by_role("button", name=STEAM_LOGIN_PATTERN).first,
            self.page.locator("input[type='password']").first,
        ]
        return self.first_visible(guest_locators, timeout_ms=1_500) is None

    def download_remote_image(self, image_url: str, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = target_path.suffix or self.guess_suffix(image_url, default=".jpg")
        final_path = target_path if target_path.suffix else target_path.with_suffix(suffix)

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
        with urllib.request.urlopen(request, timeout=30) as response:
            final_path.write_bytes(response.read())

        self.logger.info("Imagen descargada desde %s a %s.", image_url, final_path)
        return final_path

    def wait_for_page_ready(self) -> None:
        assert self.page is not None

        self.page.wait_for_load_state("domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            self.logger.info("Steam no entro en networkidle. Se continua con timeout controlado.")

        self.page.locator("body").wait_for(state="visible", timeout=10_000)

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

    def guess_suffix(self, url: str, default: str = ".jpg") -> str:
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix
        if suffix:
            return suffix

        mime_type, _ = mimetypes.guess_type(url)
        if mime_type:
            guessed = mimetypes.guess_extension(mime_type)
            if guessed:
                return guessed

        return default

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
        self.logger.info("Chromium visible iniciado para Steam.")
