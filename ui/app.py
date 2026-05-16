from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction, QFont
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
    QVBoxLayout,
    QWidget,
)

from core.history import HistoryStore
from core.input import PatchedInput
from core.runner import DailyCasesRunner
from core.runtime import RuntimePaths, configure_logging, ensure_runtime_dirs


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
    prompt_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._answer = ""

    def ask(self, prompt: str) -> str:
        with self._lock:
            self._answer = ""
            self._event.clear()
            self.prompt_requested.emit(prompt)
            self._event.wait()
            return self._answer

    def submit_answer(self, answer: str) -> None:
        self._answer = answer
        self._event.set()


class QtInputProvider:
    def __init__(self, bridge: PromptBridge) -> None:
        self.bridge = bridge

    def ask(self, prompt: str) -> str:
        return self.bridge.ask(prompt)


class RunnerThread(QThread):
    run_finished = Signal(object)
    run_failed = Signal(str)

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
            logger = configure_logging(paths.log_file, extra_handlers=[self.log_handler])
            history_store = HistoryStore(paths.db_file)
            history_store.initialize()
            runner = DailyCasesRunner(paths, logger, history_store=history_store)
            with PatchedInput(QtInputProvider(self.prompt_bridge)):
                summary = runner.run()
            self.run_finished.emit(summary)
        except Exception as exc:
            self.run_failed.emit(str(exc))


class DashboardWindow(QMainWindow):
    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self.base_dir = base_dir.resolve()
        self.paths = RuntimePaths.from_base_dir(self.base_dir)
        ensure_runtime_dirs(self.paths)
        self.history_store = HistoryStore(self.paths.db_file)
        self.history_store.initialize()

        self.log_emitter = LogEmitter()
        self.log_handler = QtLogHandler(self.log_emitter)
        self.prompt_bridge = PromptBridge()
        self.runner_thread: RunnerThread | None = None

        self.setWindowTitle("Daily Cases Bot")
        self.resize(1360, 920)
        self._build_ui()
        self._wire_signals()
        self.refresh_dashboard()

    def _build_ui(self) -> None:
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        root_layout = QVBoxLayout(central_widget)

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

        root_layout.addWidget(summary_group)
        root_layout.addLayout(actions_layout)
        root_layout.addWidget(top_splitter, stretch=1)
        root_layout.addWidget(bottom_splitter, stretch=1)

        exit_action = QAction("Salir", self)
        exit_action.triggered.connect(self.close)
        self.addAction(exit_action)

    def _wire_signals(self) -> None:
        self.run_button.clicked.connect(self.start_run)
        self.refresh_button.clicked.connect(self.refresh_dashboard)
        self.log_emitter.message.connect(self.append_log)
        self.prompt_bridge.prompt_requested.connect(self.show_prompt_dialog)

    def _make_value_label(self, text: str) -> QLabel:
        label = QLabel(text)
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        label.setFont(font)
        return label

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
        self.runner_thread = RunnerThread(
            self.base_dir,
            self.prompt_bridge,
            self.log_handler,
        )
        self.runner_thread.run_finished.connect(self.on_run_finished)
        self.runner_thread.run_failed.connect(self.on_run_failed)
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

    def refresh_dashboard(self) -> None:
        self.total_balance_label.setText(self.format_amount(self.load_total_balance()))
        self.today_total_label.setText(
            self.format_amount(self.history_store.get_today_positive_total())
        )

        last_run = self.history_store.get_last_run_finished_at()
        self.last_run_label.setText(last_run or "Sin ejecuciones registradas")

        latest_site_rows = {
            row["site_name"]: row
            for row in self.history_store.get_latest_site_results()
        }
        ordered_sites = ["keydrop", "csgocases", "bloodycase", "cs2free"]
        self.site_table.setRowCount(len(ordered_sites))
        for row_index, site_name in enumerate(ordered_sites):
            row = latest_site_rows.get(site_name, {})
            self.set_table_item(self.site_table, row_index, 0, site_name)
            self.set_table_item(self.site_table, row_index, 1, str(row.get("status", "-")))
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

    def append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)
        self.log_output.verticalScrollBar().setValue(
            self.log_output.verticalScrollBar().maximum()
        )

    def show_prompt_dialog(self, prompt: str) -> None:
        text, accepted = QInputDialog.getText(
            self,
            "Intervencion requerida",
            prompt,
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
