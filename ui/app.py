from __future__ import annotations

import json
import logging
import threading
import unicodedata
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.history import HistoryStore
from core.runner import DailyCasesRunner
from core.runtime import RuntimePaths, configure_logging, ensure_runtime_dirs
from interaction import PromptRequest, reset_interaction_provider, set_interaction_provider
from steam_status import (
    configure_steam_status_store,
    get_steam_status_snapshot,
    set_steam_status_callback,
)


class LogEmitter(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self.emitter = emitter
        self.set_name("daily_cases_qt")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self.emitter.message.emit(message)


class PromptBridge(QObject):
    prompt_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._answer = ""

    def ask(self, request: PromptRequest) -> str:
        with self._lock:
            self._answer = ""
            self._event.clear()
            self.prompt_requested.emit(request)
            self._event.wait()
            return self._answer

    def submit_answer(self, answer: str) -> None:
        self._answer = answer
        self._event.set()


class QtInputProvider:
    def __init__(self, bridge: PromptBridge) -> None:
        self.bridge = bridge

    def ask(self, request: PromptRequest) -> str:
        return self.bridge.ask(request)


class RunnerThread(QThread):
    run_finished = Signal(object)
    run_failed = Signal(str)
    progress_updated = Signal(object)
    steam_status_updated = Signal(object)

    def __init__(
        self,
        base_dir: Path,
        prompt_bridge: PromptBridge,
        log_handler: logging.Handler,
    ) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.prompt_bridge = prompt_bridge
        self.log_handler = log_handler

    def run(self) -> None:
        try:
            paths = RuntimePaths.from_base_dir(self.base_dir)
            ensure_runtime_dirs(paths)
            configure_steam_status_store(paths.steam_state_file)
            logger = configure_logging(paths.log_file, extra_handlers=[self.log_handler])
            history_store = HistoryStore(paths.db_file)
            history_store.initialize()
            runner = DailyCasesRunner(
                paths,
                logger,
                history_store=history_store,
                progress_callback=self.progress_updated.emit,
            )
            set_interaction_provider(QtInputProvider(self.prompt_bridge))
            set_steam_status_callback(self.steam_status_updated.emit)
            summary = runner.run()
            self.run_finished.emit(summary)
        except Exception as exc:
            self.run_failed.emit(str(exc))
        finally:
            reset_interaction_provider()
            set_steam_status_callback(None)


class DashboardWindow(QMainWindow):
    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self.base_dir = base_dir.resolve()
        self.paths = RuntimePaths.from_base_dir(self.base_dir)
        ensure_runtime_dirs(self.paths)
        configure_steam_status_store(self.paths.steam_state_file)
        self.history_store = HistoryStore(self.paths.db_file)
        self.history_store.initialize()

        self.log_emitter = LogEmitter()
        self.log_handler = QtLogHandler(self.log_emitter)
        self.prompt_bridge = PromptBridge()
        self.runner_thread: RunnerThread | None = None
        self.current_run_progress: dict[str, dict[str, str]] = {}
        self.diagnostic_rows: list[dict[str, object]] = []

        self.setWindowTitle("Daily Cases Bot")
        self.resize(1360, 920)
        self._build_ui()
        self._wire_signals()
        self.refresh_dashboard()

    def _build_ui(self) -> None:
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        root_layout = QVBoxLayout(central_widget)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)

        dashboard_tab = QWidget()
        dashboard_layout = QVBoxLayout(dashboard_tab)

        header_layout = QHBoxLayout()

        summary_group = QGroupBox("Resumen")
        summary_layout = QGridLayout(summary_group)
        self.total_balance_label = self._make_value_label("0,00")
        self.today_total_label = self._make_value_label("0,00")
        self.last_run_label = QLabel("Sin ejecuciones registradas")
        self.last_run_label.setWordWrap(True)

        summary_layout.addWidget(QLabel("Saldo total"), 0, 0)
        summary_layout.addWidget(self.total_balance_label, 0, 1)
        summary_layout.addWidget(QLabel("Acumulado hoy"), 1, 0)
        summary_layout.addWidget(self.today_total_label, 1, 1)
        summary_layout.addWidget(QLabel("Ultima ejecucion"), 2, 0)
        summary_layout.addWidget(self.last_run_label, 2, 1)
        header_layout.addWidget(summary_group, stretch=2)

        steam_group = QGroupBox("Steam")
        steam_layout = QHBoxLayout(steam_group)
        self.steam_avatar_label = QLabel("Sin avatar")
        self.steam_avatar_label.setAlignment(Qt.AlignCenter)
        self.steam_avatar_label.setFixedSize(128, 128)
        self.steam_avatar_label.setStyleSheet(
            "background-color: #f3f3f3; border: 1px solid #bdbdbd; border-radius: 10px; color: #111111;"
        )
        steam_layout.addWidget(self.steam_avatar_label)

        steam_info_layout = QVBoxLayout()
        self.steam_profile_name_label = QLabel("-")
        self.steam_profile_name_label.setFont(self._make_name_font())
        self.steam_profile_name_label.setWordWrap(True)
        self.steam_profile_name_label.setStyleSheet("color: #111111;")

        self.steam_profile_status_label = QLabel("Steam profile")
        self.steam_profile_status_label.setStyleSheet("color: #333333; font-size: 12px;")

        self.steam_playtime_label = QLabel("-")
        self.steam_playtime_label.setStyleSheet("color: #111111; font-weight: bold;")

        badges_layout = QHBoxLayout()
        self.steam_avatar_status_label = QLabel("Avatar actual")
        self.steam_avatar_status_label.setAlignment(Qt.AlignCenter)
        self.steam_avatar_status_label.setStyleSheet(self._steam_badge_style("#2d3f50", "#c7d5e0"))
        badges_layout.addWidget(self.steam_avatar_status_label)

        self.steam_profile_mode_label = QLabel("Nick actual")
        self.steam_profile_mode_label.setAlignment(Qt.AlignCenter)
        self.steam_profile_mode_label.setStyleSheet(self._steam_badge_style("#2d3f50", "#c7d5e0"))
        badges_layout.addWidget(self.steam_profile_mode_label)
        badges_layout.addStretch(1)

        steam_info_layout.addWidget(self.steam_profile_name_label)
        steam_info_layout.addWidget(self.steam_profile_status_label)
        steam_info_layout.addSpacing(4)
        steam_info_layout.addWidget(self.steam_playtime_label)
        steam_info_layout.addSpacing(8)
        steam_info_layout.addLayout(badges_layout)
        steam_info_layout.addStretch(1)
        steam_layout.addLayout(steam_info_layout, stretch=1)
        header_layout.addWidget(steam_group, stretch=1)

        actions_layout = QHBoxLayout()
        self.run_button = QPushButton("Ejecutar flujo completo")
        self.refresh_button = QPushButton("Actualizar panel")
        actions_layout.addWidget(self.run_button)
        actions_layout.addWidget(self.refresh_button)
        actions_layout.addStretch(1)

        self.site_table = QTableWidget(4, 6)
        self.site_table.setHorizontalHeaderLabels(
            ["Sitio", "Estado", "Saldo", "Recompensa", "Tipo", "Delta"]
        )
        self.site_table.verticalHeader().setVisible(False)
        self.site_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.site_table.setSelectionMode(QTableWidget.NoSelection)
        self.site_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.daily_table = QTableWidget(0, 2)
        self.daily_table.setHorizontalHeaderLabels(["Fecha", "Acumulado"])
        self.daily_table.verticalHeader().setVisible(False)
        self.daily_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.daily_table.setSelectionMode(QTableWidget.NoSelection)
        self.daily_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.recent_table = QTableWidget(0, 5)
        self.recent_table.setHorizontalHeaderLabels(
            ["Fecha", "Sitio", "Estado", "Recompensa", "Delta"]
        )
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.recent_table.setSelectionMode(QTableWidget.NoSelection)
        self.recent_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        top_splitter = QSplitter(Qt.Horizontal)
        site_group = QGroupBox("Estado actual por sitio")
        site_layout = QVBoxLayout(site_group)
        site_layout.addWidget(self.site_table)

        daily_group = QGroupBox("Acumulado diario")
        daily_layout = QVBoxLayout(daily_group)
        daily_layout.addWidget(self.daily_table)

        top_splitter.addWidget(site_group)
        top_splitter.addWidget(daily_group)
        top_splitter.setStretchFactor(0, 2)
        top_splitter.setStretchFactor(1, 1)

        bottom_splitter = QSplitter(Qt.Horizontal)
        recent_group = QGroupBox("Actividad reciente")
        recent_layout = QVBoxLayout(recent_group)
        recent_layout.addWidget(self.recent_table)

        log_group = QGroupBox("Logs")
        log_layout = QVBoxLayout(log_group)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        log_layout.addWidget(self.log_output)

        bottom_splitter.addWidget(recent_group)
        bottom_splitter.addWidget(log_group)
        bottom_splitter.setStretchFactor(0, 1)
        bottom_splitter.setStretchFactor(1, 1)

        dashboard_layout.addLayout(header_layout)
        dashboard_layout.addLayout(actions_layout)
        dashboard_layout.addWidget(top_splitter, stretch=1)
        dashboard_layout.addWidget(bottom_splitter, stretch=1)
        self.tabs.addTab(dashboard_tab, "Panel")

        diagnostics_tab = QWidget()
        diagnostics_layout = QVBoxLayout(diagnostics_tab)

        diagnostics_actions_layout = QHBoxLayout()
        self.open_diagnostic_image_button = QPushButton("Abrir captura")
        self.open_diagnostic_text_button = QPushButton("Abrir texto")
        self.open_diagnostic_json_button = QPushButton("Abrir JSON")
        diagnostics_actions_layout.addWidget(self.open_diagnostic_image_button)
        diagnostics_actions_layout.addWidget(self.open_diagnostic_text_button)
        diagnostics_actions_layout.addWidget(self.open_diagnostic_json_button)
        diagnostics_actions_layout.addStretch(1)

        self.diagnostics_table = QTableWidget(0, 5)
        self.diagnostics_table.setHorizontalHeaderLabels(
            ["Fecha", "Sitio", "Estado", "Recompensa", "Archivo"]
        )
        self.diagnostics_table.verticalHeader().setVisible(False)
        self.diagnostics_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.diagnostics_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.diagnostics_table.setSelectionMode(QTableWidget.SingleSelection)
        self.diagnostics_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )

        self.diagnostic_files_label = QLabel("Selecciona un diagnostico para ver sus archivos.")
        self.diagnostic_files_label.setWordWrap(True)

        self.diagnostic_preview = QPlainTextEdit()
        self.diagnostic_preview.setReadOnly(True)

        diagnostics_layout.addLayout(diagnostics_actions_layout)
        diagnostics_layout.addWidget(self.diagnostics_table, stretch=1)
        diagnostics_layout.addWidget(self.diagnostic_files_label)
        diagnostics_layout.addWidget(self.diagnostic_preview, stretch=1)
        self.tabs.addTab(diagnostics_tab, "Diagnosticos")

        exit_action = QAction("Salir", self)
        exit_action.triggered.connect(self.close)
        self.addAction(exit_action)
        self.update_diagnostics_actions()

    def _wire_signals(self) -> None:
        self.run_button.clicked.connect(self.start_run)
        self.refresh_button.clicked.connect(self.refresh_dashboard)
        self.log_emitter.message.connect(self.append_log)
        self.prompt_bridge.prompt_requested.connect(self.show_prompt_dialog)
        self.diagnostics_table.itemSelectionChanged.connect(
            self.on_diagnostic_selection_changed
        )
        self.open_diagnostic_image_button.clicked.connect(
            lambda: self.open_selected_diagnostic_file("image")
        )
        self.open_diagnostic_text_button.clicked.connect(
            lambda: self.open_selected_diagnostic_file("text")
        )
        self.open_diagnostic_json_button.clicked.connect(
            lambda: self.open_selected_diagnostic_file("json")
        )
        self.update_steam_panel(get_steam_status_snapshot())

    def _make_value_label(self, text: str) -> QLabel:
        label = QLabel(text)
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        label.setFont(font)
        return label

    def _make_name_font(self) -> QFont:
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        font.setFamilies(
            [
                "Noto Sans Sinhala",
                "Noto Sans",
                "DejaVu Sans",
                "Sans Serif",
            ]
        )
        return font

    def _steam_badge_style(self, background: str, foreground: str) -> str:
        return (
            f"background-color: {background}; color: {foreground}; "
            "border-radius: 8px; padding: 4px 10px; font-size: 11px; font-weight: bold;"
        )

    def start_run(self) -> None:
        if self.runner_thread is not None and self.runner_thread.isRunning():
            QMessageBox.information(
                self,
                "Ejecucion en curso",
                "Ya hay una ejecucion en curso.",
            )
            return

        self.append_log("Iniciando ejecucion desde la interfaz.")
        self.run_button.setEnabled(False)
        self.current_run_progress = {
            site_name: {"site_name": site_name, "phase": "Pendiente", "result": "-"}
            for site_name in ("keydrop", "csgocases", "bloodycase", "cs2free")
        }
        self.refresh_dashboard()
        self.runner_thread = RunnerThread(
            self.base_dir,
            self.prompt_bridge,
            self.log_handler,
        )
        self.runner_thread.run_finished.connect(self.on_run_finished)
        self.runner_thread.run_failed.connect(self.on_run_failed)
        self.runner_thread.progress_updated.connect(self.update_run_progress)
        self.runner_thread.steam_status_updated.connect(self.update_steam_panel)
        self.runner_thread.finished.connect(self.on_runner_thread_finished)
        self.runner_thread.start()

    def on_run_finished(self, summary: object) -> None:
        self.append_log("Ejecucion finalizada. Refrescando panel.")
        self.refresh_dashboard()

    def on_run_failed(self, error_message: str) -> None:
        QMessageBox.critical(
            self,
            "Error en la ejecucion",
            error_message or "La ejecucion ha fallado sin mensaje adicional.",
        )
        self.append_log(f"Error al ejecutar el flujo: {error_message}")

    def on_runner_thread_finished(self) -> None:
        self.run_button.setEnabled(True)

    def update_run_progress(self, progress_rows: object) -> None:
        if not isinstance(progress_rows, list):
            return
        self.current_run_progress = {
            str(row.get("site_name")): row
            for row in progress_rows
            if isinstance(row, dict)
        }
        self.refresh_dashboard()

    def refresh_dashboard(self) -> None:
        self.total_balance_label.setText(self.format_amount(self.load_total_balance()))
        self.today_total_label.setText(
            self.format_amount(self.history_store.get_today_positive_total())
        )
        self.update_steam_panel(get_steam_status_snapshot())

        last_run = self.history_store.get_last_run_finished_at()
        self.last_run_label.setText(last_run or "Sin ejecuciones registradas")

        latest_site_rows = {
            row["site_name"]: row
            for row in self.history_store.get_latest_site_results()
        }
        self.refresh_site_table(latest_site_rows)

        daily_rows = self.history_store.get_daily_totals(30)
        self.daily_table.setRowCount(len(daily_rows))
        for row_index, row in enumerate(daily_rows):
            self.set_table_item(self.daily_table, row_index, 0, str(row["day"]))
            self.set_table_item(
                self.daily_table,
                row_index,
                1,
                self.format_amount(row["total"]),
            )

        recent_rows = self.history_store.get_recent_site_results(40)
        self.recent_table.setRowCount(len(recent_rows))
        for row_index, row in enumerate(recent_rows):
            self.set_table_item(
                self.recent_table,
                row_index,
                0,
                str(row.get("created_at") or "-"),
            )
            self.set_table_item(
                self.recent_table,
                row_index,
                1,
                str(row.get("site_name") or "-"),
            )
            self.set_table_item(
                self.recent_table,
                row_index,
                2,
                str(row.get("status") or "-"),
            )
            reward_text = str(row.get("reward_text") or "-")
            reward_kind = str(row.get("reward_kind") or "")
            if reward_kind and reward_kind != "unknown" and reward_text != "-":
                reward_text = f"{reward_text} [{reward_kind}]"
            self.set_table_item(self.recent_table, row_index, 3, reward_text)
            self.set_table_item(
                self.recent_table,
                row_index,
                4,
                self.format_amount(row.get("balance_delta")),
            )

        self.refresh_diagnostics_table()

    def refresh_site_table(self, latest_site_rows: dict[str, dict[str, object]]) -> None:
        ordered_sites = ["keydrop", "csgocases", "bloodycase", "cs2free"]
        self.site_table.setRowCount(len(ordered_sites))
        for row_index, site_name in enumerate(ordered_sites):
            row = latest_site_rows.get(site_name, {})
            progress = self.current_run_progress.get(site_name, {})

            status_text = str(row.get("status", "-"))
            if progress:
                phase = str(progress.get("phase") or "Pendiente")
                result = str(progress.get("result") or "-")
                if phase == "En curso":
                    status_text = "En curso"
                elif phase == "Pendiente":
                    status_text = "Pendiente"
                elif phase == "Hecho" and result and result != "-":
                    status_text = result

            self.set_table_item(self.site_table, row_index, 0, site_name)
            self.set_table_item(self.site_table, row_index, 1, status_text)
            self.set_table_item(
                self.site_table,
                row_index,
                2,
                str(row.get("balance_text") or "-"),
            )
            self.set_table_item(
                self.site_table,
                row_index,
                3,
                str(row.get("reward_text") or "-"),
            )
            self.set_table_item(
                self.site_table,
                row_index,
                4,
                str(row.get("reward_kind") or "-"),
            )
            self.set_table_item(
                self.site_table,
                row_index,
                5,
                self.format_amount(row.get("balance_delta")),
            )
            self.apply_site_status_color(row_index, progress)

    def apply_site_status_color(
        self,
        row_index: int,
        progress: dict[str, str],
    ) -> None:
        status_item = self.site_table.item(row_index, 1)
        if status_item is None:
            return

        status_item.setForeground(QColor("#111111"))
        status_item.setBackground(QColor("#ffffff"))

        if not progress:
            return

        phase = str(progress.get("phase") or "")
        if phase == "Pendiente":
            status_item.setBackground(QColor("#f4e7a3"))
        elif phase == "En curso":
            status_item.setBackground(QColor("#9bd1ff"))
        elif phase == "Hecho":
            result = str(progress.get("result") or "").strip().lower()
            if result == "cooldown":
                status_item.setBackground(QColor("#d9d9d9"))
            elif result == "opened_sold":
                status_item.setBackground(QColor("#5fd46b"))
            elif result == "aborted":
                status_item.setBackground(QColor("#f28b82"))
            elif result == "account_setup_required":
                status_item.setBackground(QColor("#f6c177"))
            else:
                status_item.setBackground(QColor("#a9e5b0"))

    def load_total_balance(self) -> float:
        if not self.paths.balances_file.exists():
            return 0.0
        try:
            store = json.loads(self.paths.balances_file.read_text(encoding="utf-8"))
        except Exception:
            return 0.0

        total = 0.0
        for site_payload in store.values():
            if not isinstance(site_payload, dict):
                continue
            latest = site_payload.get("latest")
            if not isinstance(latest, dict):
                continue
            value = latest.get("balance_value")
            if isinstance(value, (int, float)):
                total += float(value)
        return round(total, 2)

    def update_steam_panel(self, steam_status: object) -> None:
        if not isinstance(steam_status, dict):
            return

        self.steam_playtime_label.setText(
            f"CS2 ultimas 2 semanas: {steam_status.get('recent_hours_text') or '-'}"
        )

        avatar_temporary = bool(steam_status.get("avatar_temporary"))
        self.steam_avatar_status_label.setText(
            "Avatar temporal" if avatar_temporary else "Avatar actual"
        )
        self.steam_avatar_status_label.setStyleSheet(
            self._steam_badge_style(
                "#cfe8ff" if avatar_temporary else "#e9e9e9",
                "#111111",
            )
        )

        profile_name = self.normalize_display_name(
            str(steam_status.get("profile_name") or "-")
        )
        self.steam_profile_name_label.setText(profile_name or "-")
        profile_temporary = bool(steam_status.get("profile_name_temporary"))
        self.steam_profile_mode_label.setText(
            "Nick temporal" if profile_temporary else "Nick actual"
        )
        self.steam_profile_mode_label.setStyleSheet(
            self._steam_badge_style(
                "#cfe8ff" if profile_temporary else "#e9e9e9",
                "#111111",
            )
        )
        self.steam_profile_status_label.setText(
            "Steam profile temporal" if (avatar_temporary or profile_temporary) else "Steam profile actual"
        )

        avatar_path_value = steam_status.get("avatar_path")
        if not avatar_path_value:
            self.steam_avatar_label.setText("Sin avatar")
            self.steam_avatar_label.setPixmap(QPixmap())
            return

        avatar_path = Path(str(avatar_path_value))
        if not avatar_path.exists():
            self.steam_avatar_label.setText("Sin avatar")
            self.steam_avatar_label.setPixmap(QPixmap())
            return

        pixmap = QPixmap(str(avatar_path))
        if pixmap.isNull():
            self.steam_avatar_label.setText("Sin avatar")
            self.steam_avatar_label.setPixmap(QPixmap())
            return

        scaled = pixmap.scaled(
            self.steam_avatar_label.width(),
            self.steam_avatar_label.height(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.steam_avatar_label.setText("")
        self.steam_avatar_label.setPixmap(scaled)

    def normalize_display_name(self, value: str) -> str:
        cleaned_chars: list[str] = []
        for char in value:
            category = unicodedata.category(char)
            if category == "Cf":
                continue
            cleaned_chars.append(char)
        cleaned = "".join(cleaned_chars).strip()
        return " ".join(cleaned.split())

    def append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)
        self.log_output.verticalScrollBar().setValue(
            self.log_output.verticalScrollBar().maximum()
        )

    def refresh_diagnostics_table(self) -> None:
        previous_row_index = self.diagnostics_table.currentRow()
        self.diagnostic_rows = self.history_store.get_recent_diagnostics(80)
        self.diagnostics_table.setRowCount(len(self.diagnostic_rows))
        for row_index, row in enumerate(self.diagnostic_rows):
            reward_text = str(row.get("reward_text") or "-")
            reward_kind = str(row.get("reward_kind") or "")
            if reward_kind and reward_kind != "unknown" and reward_text != "-":
                reward_text = f"{reward_text} [{reward_kind}]"

            diagnostic_name = "-"
            diagnostic_path_value = row.get("diagnostic_json_path")
            if diagnostic_path_value:
                diagnostic_name = Path(str(diagnostic_path_value)).name

            self.set_table_item(
                self.diagnostics_table,
                row_index,
                0,
                str(row.get("created_at") or "-"),
            )
            self.set_table_item(
                self.diagnostics_table,
                row_index,
                1,
                str(row.get("site_name") or "-"),
            )
            self.set_table_item(
                self.diagnostics_table,
                row_index,
                2,
                str(row.get("status") or "-"),
            )
            self.set_table_item(self.diagnostics_table, row_index, 3, reward_text)
            self.set_table_item(self.diagnostics_table, row_index, 4, diagnostic_name)

        if self.diagnostic_rows:
            target_row = previous_row_index if previous_row_index >= 0 else 0
            target_row = min(target_row, len(self.diagnostic_rows) - 1)
            self.diagnostics_table.selectRow(target_row)
            self.on_diagnostic_selection_changed()
        else:
            self.diagnostic_files_label.setText(
                "No hay diagnosticos guardados todavia."
            )
            self.diagnostic_preview.clear()
            self.update_diagnostics_actions()

    def on_diagnostic_selection_changed(self) -> None:
        row = self.get_selected_diagnostic_row()
        if row is None:
            self.diagnostic_files_label.setText(
                "Selecciona un diagnostico para ver sus archivos."
            )
            self.diagnostic_preview.clear()
            self.update_diagnostics_actions()
            return

        json_path, image_path, text_path = self.get_diagnostic_paths(row)
        file_parts = [
            f"JSON: {json_path.name if json_path else '-'}",
            f"PNG: {image_path.name if image_path else '-'}",
            f"TXT: {text_path.name if text_path else '-'}",
        ]
        self.diagnostic_files_label.setText(" | ".join(file_parts))

        preview_text = ""
        preview_path = text_path if text_path and text_path.exists() else json_path
        if preview_path and preview_path.exists():
            try:
                preview_text = preview_path.read_text(encoding="utf-8")
            except Exception as exc:
                preview_text = f"No se pudo leer {preview_path.name}: {exc}"

        self.diagnostic_preview.setPlainText(preview_text[:12_000])
        self.update_diagnostics_actions()

    def update_diagnostics_actions(self) -> None:
        row = self.get_selected_diagnostic_row()
        json_path, image_path, text_path = self.get_diagnostic_paths(row)
        self.open_diagnostic_image_button.setEnabled(
            image_path is not None and image_path.exists()
        )
        self.open_diagnostic_text_button.setEnabled(
            text_path is not None and text_path.exists()
        )
        self.open_diagnostic_json_button.setEnabled(
            json_path is not None and json_path.exists()
        )

    def get_selected_diagnostic_row(self) -> dict[str, object] | None:
        row_index = self.diagnostics_table.currentRow()
        if row_index < 0 or row_index >= len(self.diagnostic_rows):
            return None
        return self.diagnostic_rows[row_index]

    def get_diagnostic_paths(
        self,
        row: dict[str, object] | None,
    ) -> tuple[Path | None, Path | None, Path | None]:
        if not row:
            return None, None, None

        diagnostic_json_path = row.get("diagnostic_json_path")
        if not diagnostic_json_path:
            return None, None, None

        json_path = Path(str(diagnostic_json_path))
        image_path = json_path.with_suffix(".png")
        text_path = json_path.with_suffix(".txt")
        return json_path, image_path, text_path

    def open_selected_diagnostic_file(self, file_kind: str) -> None:
        row = self.get_selected_diagnostic_row()
        json_path, image_path, text_path = self.get_diagnostic_paths(row)

        target_path: Path | None
        if file_kind == "image":
            target_path = image_path
        elif file_kind == "text":
            target_path = text_path
        else:
            target_path = json_path

        if target_path is None or not target_path.exists():
            QMessageBox.warning(
                self,
                "Archivo no disponible",
                "El archivo seleccionado no existe o aun no se ha generado.",
            )
            self.update_diagnostics_actions()
            return

        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_path.resolve()))):
            QMessageBox.warning(
                self,
                "No se pudo abrir",
                f"No se pudo abrir {target_path.name}.",
            )

    def show_prompt_dialog(self, request: PromptRequest) -> None:
        text, accepted = QInputDialog.getText(
            self,
            request.title,
            request.message,
            text=request.default,
        )
        if not accepted:
            self.prompt_bridge.submit_answer("q")
            return
        self.prompt_bridge.submit_answer(text)

    def set_table_item(
        self,
        table: QTableWidget,
        row: int,
        column: int,
        value: str,
    ) -> None:
        table.setItem(row, column, QTableWidgetItem(value))

    def format_amount(self, value: object) -> str:
        if value is None:
            return "-"
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"{numeric_value:.2f} US$"


def launch_app(base_dir: Path) -> int:
    app = QApplication.instance() or QApplication([])
    window = DashboardWindow(base_dir)
    window.show()
    return app.exec()
