from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

SITE_ORDER = ["keydrop", "csgocases", "bloodycase", "cs2free", "g4skins", "dropland"]
TAB_OPTIONS = [
    "Panel",
    "Primera ejecucion",
    "Historico",
    "Diagnosticos",
    "Configuracion",
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "flow": {
        "enabled_sites": list(SITE_ORDER),
    },
    "steam": {
        "use_presence_during_run": True,
        "headless_profile_refresh": True,
    },
    "interface": {
        "initial_tab": "Panel",
        "auto_focus_setup_on_issues": True,
        "remember_last_tab": True,
        "last_tab": "Panel",
        "recent_activity_limit": 40,
        "runs_limit": 40,
        "diagnostics_limit": 80,
        "daily_totals_limit": 30,
        "window_width": 1360,
        "window_height": 920,
    },
    "data": {
        "diagnostic_preview_chars": 12_000,
    },
}


class SettingsStore:
    def __init__(self, settings_file: Path) -> None:
        self.settings_file = settings_file

    def load(self) -> dict[str, Any]:
        if not self.settings_file.exists():
            settings = deepcopy(DEFAULT_SETTINGS)
            self.save(settings)
            return settings

        try:
            payload = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except Exception:
            settings = deepcopy(DEFAULT_SETTINGS)
            self.save(settings)
            return settings

        return self.normalize(payload if isinstance(payload, dict) else {})

    def save(self, settings: dict[str, Any]) -> None:
        normalized = self.normalize(settings)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def reset(self) -> dict[str, Any]:
        settings = deepcopy(DEFAULT_SETTINGS)
        self.save(settings)
        return settings

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = deepcopy(DEFAULT_SETTINGS)

        flow = payload.get("flow")
        if isinstance(flow, dict):
            settings["flow"]["enabled_sites"] = self._normalize_enabled_sites(
                flow.get("enabled_sites")
            )

        steam = payload.get("steam")
        if isinstance(steam, dict):
            settings["steam"]["use_presence_during_run"] = bool(
                steam.get("use_presence_during_run", True)
            )
            settings["steam"]["headless_profile_refresh"] = bool(
                steam.get("headless_profile_refresh", True)
            )

        interface = payload.get("interface")
        if isinstance(interface, dict):
            initial_tab = str(interface.get("initial_tab") or "Panel")
            last_tab = str(interface.get("last_tab") or "Panel")
            settings["interface"]["initial_tab"] = (
                initial_tab if initial_tab in TAB_OPTIONS else "Panel"
            )
            settings["interface"]["last_tab"] = (
                last_tab if last_tab in TAB_OPTIONS else "Panel"
            )
            settings["interface"]["auto_focus_setup_on_issues"] = bool(
                interface.get("auto_focus_setup_on_issues", True)
            )
            settings["interface"]["remember_last_tab"] = bool(
                interface.get("remember_last_tab", True)
            )
            settings["interface"]["recent_activity_limit"] = self._clamp_int(
                interface.get("recent_activity_limit"),
                10,
                500,
                DEFAULT_SETTINGS["interface"]["recent_activity_limit"],
            )
            settings["interface"]["runs_limit"] = self._clamp_int(
                interface.get("runs_limit"),
                10,
                500,
                DEFAULT_SETTINGS["interface"]["runs_limit"],
            )
            settings["interface"]["diagnostics_limit"] = self._clamp_int(
                interface.get("diagnostics_limit"),
                10,
                500,
                DEFAULT_SETTINGS["interface"]["diagnostics_limit"],
            )
            settings["interface"]["daily_totals_limit"] = self._clamp_int(
                interface.get("daily_totals_limit"),
                7,
                365,
                DEFAULT_SETTINGS["interface"]["daily_totals_limit"],
            )
            settings["interface"]["window_width"] = self._clamp_int(
                interface.get("window_width"),
                900,
                4000,
                DEFAULT_SETTINGS["interface"]["window_width"],
            )
            settings["interface"]["window_height"] = self._clamp_int(
                interface.get("window_height"),
                700,
                3000,
                DEFAULT_SETTINGS["interface"]["window_height"],
            )

        data = payload.get("data")
        if isinstance(data, dict):
            settings["data"]["diagnostic_preview_chars"] = self._clamp_int(
                data.get("diagnostic_preview_chars"),
                1000,
                100_000,
                DEFAULT_SETTINGS["data"]["diagnostic_preview_chars"],
            )

        return settings

    def _normalize_enabled_sites(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return list(SITE_ORDER)
        selected = [str(item).strip().lower() for item in value]
        normalized = [site for site in SITE_ORDER if site in selected]
        return normalized or list(SITE_ORDER)

    def _clamp_int(
        self,
        value: Any,
        minimum: int,
        maximum: int,
        fallback: int,
    ) -> int:
        try:
            numeric_value = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(minimum, min(maximum, numeric_value))
