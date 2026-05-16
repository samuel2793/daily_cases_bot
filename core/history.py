from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import ExecutionSummary


class HistoryStore:
    def __init__(self, db_file: Path) -> None:
        self.db_file = db_file

    def initialize(self) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    recent_hours REAL,
                    run_status TEXT NOT NULL,
                    total_balance_value REAL,
                    summary_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS site_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    site_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    balance_text TEXT,
                    balance_value REAL,
                    reward_text TEXT,
                    reward_kind TEXT,
                    sell_clicked INTEGER NOT NULL DEFAULT 0,
                    gain_value REAL,
                    balance_delta REAL,
                    diagnostic_json_path TEXT,
                    source_url TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                )
                """
            )

    def record_execution(self, summary: ExecutionSummary) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    started_at,
                    finished_at,
                    recent_hours,
                    run_status,
                    total_balance_value,
                    summary_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.started_at.isoformat(),
                    summary.finished_at.isoformat(),
                    summary.recent_hours,
                    summary.run_status,
                    summary.total_balance_value,
                    json.dumps(summary.to_dict(), ensure_ascii=False),
                ),
            )
            run_id = int(cursor.lastrowid)

            for site_result in summary.site_results:
                previous_balance_value = self._read_previous_balance_value(
                    connection,
                    site_result.site_name,
                )
                balance_delta = None
                if (
                    site_result.balance_value is not None
                    and previous_balance_value is not None
                ):
                    balance_delta = round(
                        site_result.balance_value - previous_balance_value,
                        2,
                    )

                connection.execute(
                    """
                    INSERT INTO site_results (
                        run_id,
                        site_name,
                        status,
                        balance_text,
                        balance_value,
                        reward_text,
                        reward_kind,
                        sell_clicked,
                        gain_value,
                        balance_delta,
                        diagnostic_json_path,
                        source_url,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        site_result.site_name,
                        site_result.status,
                        site_result.balance_text,
                        site_result.balance_value,
                        site_result.reward_text,
                        site_result.reward_kind,
                        1 if site_result.sell_clicked else 0,
                        site_result.gain_value,
                        balance_delta,
                        site_result.diagnostic_json_path,
                        site_result.source_url,
                        summary.finished_at.isoformat(),
                    ),
                )
                site_result.balance_delta = balance_delta

        return run_id

    def get_today_positive_total(self) -> float:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT ROUND(COALESCE(SUM(
                    CASE
                        WHEN balance_delta > 0 THEN balance_delta
                        ELSE 0
                    END
                ), 0), 2)
                FROM site_results
                WHERE DATE(created_at, 'localtime') = DATE('now', 'localtime')
                """
            ).fetchone()
        return float(row[0] or 0.0)

    def get_daily_totals(self, limit: int = 30) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    DATE(created_at, 'localtime') AS day,
                    ROUND(COALESCE(SUM(
                        CASE
                            WHEN balance_delta > 0 THEN balance_delta
                            ELSE 0
                        END
                    ), 0), 2) AS total
                FROM site_results
                GROUP BY day
                ORDER BY day DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{"day": row[0], "total": float(row[1] or 0.0)} for row in rows]

    def get_latest_site_results(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    sr.site_name,
                    sr.status,
                    sr.balance_text,
                    sr.balance_value,
                    sr.reward_text,
                    sr.reward_kind,
                    sr.sell_clicked,
                    sr.gain_value,
                    sr.balance_delta,
                    sr.diagnostic_json_path,
                    sr.source_url,
                    sr.created_at
                FROM site_results sr
                INNER JOIN (
                    SELECT site_name, MAX(id) AS max_id
                    FROM site_results
                    GROUP BY site_name
                ) latest
                    ON sr.id = latest.max_id
                ORDER BY sr.site_name
                """
            ).fetchall()

        return [
            {
                "site_name": row[0],
                "status": row[1],
                "balance_text": row[2],
                "balance_value": row[3],
                "reward_text": row[4],
                "reward_kind": row[5],
                "sell_clicked": bool(row[6]),
                "gain_value": row[7],
                "balance_delta": row[8],
                "diagnostic_json_path": row[9],
                "source_url": row[10],
                "created_at": row[11],
            }
            for row in rows
        ]

    def get_recent_site_results(self, limit: int = 50) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    site_name,
                    status,
                    reward_text,
                    reward_kind,
                    balance_text,
                    balance_delta,
                    created_at
                FROM site_results
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "site_name": row[0],
                "status": row[1],
                "reward_text": row[2],
                "reward_kind": row[3],
                "balance_text": row[4],
                "balance_delta": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]

    def get_last_run_finished_at(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row[0])

    def _read_previous_balance_value(
        self,
        connection: sqlite3.Connection,
        site_name: str,
    ) -> float | None:
        row = connection.execute(
            """
            SELECT balance_value
            FROM site_results
            WHERE site_name = ?
              AND balance_value IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (site_name,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_file)
        connection.row_factory = sqlite3.Row
        return connection
