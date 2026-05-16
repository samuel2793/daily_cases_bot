from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SiteExecutionRecord:
    site_name: str
    status: str
    balance_text: str | None = None
    balance_value: float | None = None
    reward_text: str | None = None
    reward_kind: str = "unknown"
    sell_clicked: bool = False
    gain_value: float | None = None
    diagnostic_json_path: str | None = None
    source_url: str | None = None
    balance_delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionSummary:
    started_at: datetime
    finished_at: datetime
    recent_hours: float | None
    run_status: str
    site_results: list[SiteExecutionRecord] = field(default_factory=list)
    total_balance_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "recent_hours": self.recent_hours,
            "run_status": self.run_status,
            "total_balance_value": self.total_balance_value,
            "site_results": [site_result.to_dict() for site_result in self.site_results],
        }
