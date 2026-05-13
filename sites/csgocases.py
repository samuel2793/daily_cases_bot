from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from .steam import SteamAvatarManager

DEFAULT_URL = "https://csgocases.com/es"
DAILY_FREE_CASE_URL_ES = "https://csgocases.com/es/case/caja-gratis-2"
FREE_NICK_CASE_URL_ES = "https://csgocases.com/es/case/caja-gratis"
STEAM_NICK_SUFFIX = "CS2SKINS.GIFT"


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

        try:
            avatar_manager = self.apply_steam_avatar_requirement()
            self.wait_for_manual_case_completion(
                case_label="Caja gratis 2 de CSGOCases",
                target_url=DAILY_FREE_CASE_URL_ES,
                requirement_label="avatar de Steam con el logo de CSGOCases",
            )
            self.cleanup_steam_avatar_requirement(avatar_manager)
            avatar_manager = None

            nickname_manager = self.apply_steam_profile_name_requirement()
            self.wait_for_manual_case_completion(
                case_label="Caja gratis de CSGOCases",
                target_url=FREE_NICK_CASE_URL_ES,
                requirement_label=f"nick de Steam con '{STEAM_NICK_SUFFIX}'",
            )

            self.logger.info(
                "Flujo manual de CSGOCases finalizado. URLs objetivo: %s | %s",
                DAILY_FREE_CASE_URL_ES,
                FREE_NICK_CASE_URL_ES,
            )
            return "manual_completed"
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
            answer = input(
                f"Abre manualmente '{case_label}' fuera del navegador automatizado, resuelve todo y cuando termines pulsa Enter para continuar. "
                "Escribe 'q' para cancelar y restaurar el cambio temporal de Steam: "
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

    def human_delay(self, min_seconds: float = 0.8, max_seconds: float = 1.8) -> None:
        time.sleep(random.uniform(min_seconds, max_seconds))
