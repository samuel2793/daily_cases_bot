from __future__ import annotations

from datetime import datetime
import importlib.util
import io
import json
import logging
import re
import shutil
import threading
import unicodedata
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QSpinBox,
)

from core.history import HistoryStore
from core.runner import DailyCasesRunner
from core.runtime import RuntimePaths, configure_logging, ensure_runtime_dirs
from core.settings import SITE_ORDER, SettingsStore, TAB_OPTIONS
from interaction import PromptRequest, reset_interaction_provider, set_interaction_provider
from services import SteamPresenceService
from sites.steam import SteamAvatarManager
from sites.steam_playtime import SteamPlaytimeMonitor, format_hours_and_minutes
from steam_status import (
    configure_steam_status_store,
    get_steam_status_snapshot,
    reset_steam_presence_boot_state,
    set_steam_status_callback,
    update_steam_refreshing,
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
        presence_service: SteamPresenceService,
        settings: dict[str, object],
    ) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.prompt_bridge = prompt_bridge
        self.log_handler = log_handler
        self.presence_service = presence_service
        self.settings = settings
        self._cancel_requested = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_requested.set()

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
                cancel_requested=self._cancel_requested.is_set,
                steam_presence_service=self.presence_service,
                settings=self.settings,
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


class PreparationThread(QThread):
    preparation_finished = Signal(object)
    preparation_failed = Signal(str)
    steam_status_updated = Signal(object)

    def __init__(
        self,
        base_dir: Path,
        prompt_bridge: PromptBridge,
        log_handler: logging.Handler,
        target: str,
    ) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.prompt_bridge = prompt_bridge
        self.log_handler = log_handler
        self.target = target

    def run(self) -> None:
        try:
            paths = RuntimePaths.from_base_dir(self.base_dir)
            ensure_runtime_dirs(paths)
            configure_steam_status_store(paths.steam_state_file)
            logger = configure_logging(paths.log_file, extra_handlers=[self.log_handler])
            runner = DailyCasesRunner(paths, logger)
            set_interaction_provider(QtInputProvider(self.prompt_bridge))
            set_steam_status_callback(self.steam_status_updated.emit)
            if self.target == "all":
                result = runner.prepare_all_sessions()
            else:
                result = runner.prepare_site_session(self.target)
            self.preparation_finished.emit(result)
        except Exception as exc:
            self.preparation_failed.emit(str(exc))
        finally:
            reset_interaction_provider()
            set_steam_status_callback(None)


class PresenceCommandThread(QThread):
    command_finished = Signal(str)
    command_failed = Signal(str)

    def __init__(
        self,
        prompt_bridge: PromptBridge,
        service: SteamPresenceService,
        action: str,
    ) -> None:
        super().__init__()
        self.prompt_bridge = prompt_bridge
        self.service = service
        self.action = action

    def run(self) -> None:
        try:
            set_interaction_provider(QtInputProvider(self.prompt_bridge))
            if self.action == "start":
                self.service.start()
                self.service.wait_until_ready(timeout_seconds=15.0)
                self.command_finished.emit("Servicio de presencia iniciado.")
                return
            if self.action == "stop":
                self.service.stop()
                self.command_finished.emit("Servicio de presencia detenido.")
                return
            if self.action == "restart":
                self.service.restart()
                self.service.wait_until_ready(timeout_seconds=15.0)
                self.command_finished.emit("Servicio de presencia reiniciado.")
                return
            raise ValueError(f"Accion no soportada: {self.action}")
        except Exception as exc:
            self.command_failed.emit(str(exc))
        finally:
            reset_interaction_provider()


class SteamRefreshThread(QThread):
    refresh_finished = Signal(str)
    refresh_failed = Signal(str)
    steam_status_updated = Signal(object)

    def __init__(
        self,
        base_dir: Path,
        prompt_bridge: PromptBridge,
        log_handler: logging.Handler,
        headless: bool,
    ) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.prompt_bridge = prompt_bridge
        self.log_handler = log_handler
        self.headless = headless

    def run(self) -> None:
        try:
            paths = RuntimePaths.from_base_dir(self.base_dir)
            ensure_runtime_dirs(paths)
            configure_steam_status_store(paths.steam_state_file)
            logger = configure_logging(paths.log_file, extra_handlers=[self.log_handler])
            set_interaction_provider(QtInputProvider(self.prompt_bridge))
            set_steam_status_callback(self.steam_status_updated.emit)
            update_steam_refreshing(True)

            playtime_monitor = SteamPlaytimeMonitor(
                session_file=paths.steam_session_file,
                workspace_dir=paths.data_dir,
                data_file=paths.steam_playtime_file,
                logger=logging.getLogger("daily_cases_bot.steam"),
                headless=self.headless,
                slow_mo_ms=0 if self.headless else 90,
            )
            recent_hours = playtime_monitor.check_recent_hours_once()

            steam_manager = SteamAvatarManager(
                session_file=paths.steam_session_file,
                workspace_dir=paths.data_dir,
                logger=logging.getLogger("daily_cases_bot.steam"),
                headless=self.headless,
                slow_mo_ms=0 if self.headless else 90,
            )
            try:
                steam_manager.start()
                steam_manager.backup_current_avatar()
                steam_manager.backup_current_profile_name()
            finally:
                steam_manager.close()

            self.refresh_finished.emit(
                f"Perfil de Steam actualizado. CS2: {format_hours_and_minutes(recent_hours)}"
            )
        except Exception as exc:
            self.refresh_failed.emit(str(exc))
        finally:
            update_steam_refreshing(False)
            reset_interaction_provider()
            set_steam_status_callback(None)


class DashboardWindow(QMainWindow):
    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self.base_dir = base_dir.resolve()
        self.paths = RuntimePaths.from_base_dir(self.base_dir)
        ensure_runtime_dirs(self.paths)
        configure_steam_status_store(self.paths.steam_state_file)
        self.settings_store = SettingsStore(self.paths.settings_file)
        self.settings = self.settings_store.load()
        self.history_store = HistoryStore(self.paths.db_file)
        self.history_store.initialize()

        self.log_emitter = LogEmitter()
        self.log_handler = QtLogHandler(self.log_emitter)
        self.app_logger = configure_logging(
            self.paths.log_file,
            extra_handlers=[self.log_handler],
        )
        self.prompt_bridge = PromptBridge()
        self.runner_thread: RunnerThread | None = None
        self.preparation_thread: PreparationThread | None = None
        self.presence_thread: PresenceCommandThread | None = None
        self.steam_refresh_thread: SteamRefreshThread | None = None
        self.presence_service = SteamPresenceService(
            script_path=self.paths.steam_presence_script,
            logger=logging.getLogger("daily_cases_bot.steam_presence"),
        )
        reset_steam_presence_boot_state(
            token_detected=self.presence_service._refresh_token_file().exists()
        )
        self.current_run_progress: dict[str, dict[str, str]] = {}
        self.run_history_rows: list[dict[str, object]] = []
        self.all_diagnostic_rows: list[dict[str, object]] = []
        self.diagnostic_rows: list[dict[str, object]] = []
        self.cancel_requested_in_ui = False
        self.setup_autofocus_done = False
        self.has_non_ok_setup_status = False
        self.presence_qr_dialog: QDialog | None = None
        self.presence_qr_image_label: QLabel | None = None
        self.presence_qr_dialog_link_label: QLabel | None = None
        self.presence_qr_last_seen_url: str | None = None

        self.setWindowTitle("Daily Cases Bot")
        self.resize(
            self.get_interface_int("window_width"),
            self.get_interface_int("window_height"),
        )
        self._build_ui()
        self._wire_signals()
        self.refresh_dashboard()
        self.apply_initial_tab_preference()

    def _build_ui(self) -> None:
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        root_layout = QVBoxLayout(central_widget)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs)

        self.setup_tab = QWidget()
        setup_layout = QVBoxLayout(self.setup_tab)

        self.setup_summary_label = QLabel("Comprobando configuracion inicial...")
        self.setup_summary_label.setWordWrap(True)

        setup_actions_layout = QHBoxLayout()
        self.refresh_setup_button = QPushButton("Revisar configuracion")
        self.prepare_all_button = QPushButton("Preparar todo")
        self.open_sessions_dir_button = QPushButton("Abrir sesiones")
        self.open_logs_dir_button = QPushButton("Abrir logs")
        self.open_data_dir_button = QPushButton("Abrir datos")
        setup_actions_layout.addWidget(self.refresh_setup_button)
        setup_actions_layout.addWidget(self.prepare_all_button)
        setup_actions_layout.addWidget(self.open_sessions_dir_button)
        setup_actions_layout.addWidget(self.open_logs_dir_button)
        setup_actions_layout.addWidget(self.open_data_dir_button)
        setup_actions_layout.addStretch(1)

        prepare_buttons_layout = QHBoxLayout()
        self.prepare_steam_button = QPushButton("Preparar Steam")
        self.prepare_keydrop_button = QPushButton("Preparar KeyDrop")
        self.prepare_csgocases_button = QPushButton("Preparar CSGOCases")
        self.prepare_bloodycase_button = QPushButton("Preparar BloodyCase")
        self.prepare_cs2free_button = QPushButton("Preparar CS2.free")
        self.prepare_g4skins_button = QPushButton("Preparar G4Skins")
        prepare_buttons_layout.addWidget(self.prepare_steam_button)
        prepare_buttons_layout.addWidget(self.prepare_keydrop_button)
        prepare_buttons_layout.addWidget(self.prepare_csgocases_button)
        prepare_buttons_layout.addWidget(self.prepare_bloodycase_button)
        prepare_buttons_layout.addWidget(self.prepare_cs2free_button)
        prepare_buttons_layout.addWidget(self.prepare_g4skins_button)
        prepare_buttons_layout.addStretch(1)

        self.setup_table = QTableWidget(0, 4)
        self.setup_table.setHorizontalHeaderLabels(
            ["Categoria", "Elemento", "Estado", "Detalle"]
        )
        self.setup_table.verticalHeader().setVisible(False)
        self.setup_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setup_table.setSelectionMode(QTableWidget.NoSelection)
        self.setup_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        session_group = QGroupBox("Mantenimiento de sesiones")
        session_layout = QVBoxLayout(session_group)
        session_actions_layout = QHBoxLayout()
        self.revalidate_session_button = QPushButton("Revalidar sesion")
        self.delete_session_button = QPushButton("Borrar sesion")
        self.revalidate_session_button.setEnabled(False)
        self.delete_session_button.setEnabled(False)
        session_actions_layout.addWidget(self.revalidate_session_button)
        session_actions_layout.addWidget(self.delete_session_button)
        session_actions_layout.addStretch(1)

        self.session_table = QTableWidget(0, 4)
        self.session_table.setHorizontalHeaderLabels(
            ["Sitio", "Estado", "Actualizada", "Archivo"]
        )
        self.session_table.verticalHeader().setVisible(False)
        self.session_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.session_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.session_table.setSelectionMode(QTableWidget.SingleSelection)
        self.session_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.session_detail_label = QLabel(
            "Selecciona una sesion para revalidarla o borrarla."
        )
        self.session_detail_label.setWordWrap(True)

        session_layout.addLayout(session_actions_layout)
        session_layout.addWidget(self.session_table)
        session_layout.addWidget(self.session_detail_label)

        self.setup_login_label = QLabel(
            "La guia indicara aqui que webs pediran login manual en la primera ejecucion."
        )
        self.setup_login_label.setWordWrap(True)

        setup_layout.addWidget(self.setup_summary_label)
        setup_layout.addLayout(setup_actions_layout)
        setup_layout.addLayout(prepare_buttons_layout)
        setup_layout.addWidget(self.setup_table, stretch=1)
        setup_layout.addWidget(session_group, stretch=1)
        setup_layout.addWidget(self.setup_login_label)
        self.tabs.addTab(self.setup_tab, "Primera ejecucion")

        self.dashboard_tab = QWidget()
        dashboard_layout = QVBoxLayout(self.dashboard_tab)

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
        steam_top_layout = QHBoxLayout()
        self.steam_profile_name_label = QLabel("-")
        self.steam_profile_name_label.setFont(self._make_name_font())
        self.steam_profile_name_label.setWordWrap(True)
        self.steam_profile_name_label.setStyleSheet("color: #111111;")
        self.steam_refresh_button = QToolButton()
        self.steam_refresh_button.setToolTip("Actualizar perfil de Steam")
        self.steam_refresh_button.setAutoRaise(True)
        self.steam_refresh_button.setIcon(
            self.style().standardIcon(self.style().StandardPixmap.SP_BrowserReload)
        )
        steam_top_layout.addWidget(self.steam_profile_name_label, stretch=1)
        steam_top_layout.addWidget(self.steam_refresh_button)

        self.steam_profile_status_label = QLabel("Steam profile")
        self.steam_profile_status_label.setStyleSheet("color: #333333; font-size: 12px;")
        self.steam_updated_label = QLabel("Actualizado: -")
        self.steam_updated_label.setStyleSheet("color: #555555; font-size: 11px;")

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

        steam_info_layout.addLayout(steam_top_layout)
        steam_info_layout.addWidget(self.steam_profile_status_label)
        steam_info_layout.addWidget(self.steam_updated_label)
        steam_info_layout.addSpacing(4)
        steam_info_layout.addWidget(self.steam_playtime_label)
        steam_info_layout.addSpacing(8)
        steam_info_layout.addLayout(badges_layout)
        steam_info_layout.addStretch(1)
        steam_layout.addLayout(steam_info_layout, stretch=1)
        header_layout.addWidget(steam_group, stretch=1)

        presence_group = QGroupBox("Presencia en Steam")
        presence_layout = QVBoxLayout(presence_group)
        self.presence_status_label = QLabel("No iniciado")
        self.presence_status_label.setAlignment(Qt.AlignCenter)
        self.presence_status_label.setStyleSheet(
            self._presence_status_style("#e9e9e9", "#111111")
        )
        self.presence_detail_label = QLabel("-")
        self.presence_detail_label.setWordWrap(True)
        self.presence_detail_label.setStyleSheet("color: #333333; font-size: 11px;")
        self.presence_script_label = QLabel("Script: -")
        self.presence_script_label.setStyleSheet("color: #333333; font-size: 11px;")
        self.presence_token_label = QLabel("Refresh token: -")
        self.presence_token_label.setStyleSheet("color: #333333; font-size: 11px;")
        self.presence_qr_hint_label = QLabel("QR de autorizacion")
        self.presence_qr_hint_label.setStyleSheet("color: #333333; font-size: 11px;")
        self.presence_open_qr_button = QPushButton("Abrir QR")
        self.presence_qr_link_label = QLabel("")
        self.presence_qr_link_label.setOpenExternalLinks(True)
        self.presence_qr_link_label.setStyleSheet("color: #0b57d0; font-size: 11px;")
        presence_buttons_layout = QHBoxLayout()
        self.presence_start_button = QToolButton()
        self.presence_start_button.setToolTip("Iniciar presencia en Steam")
        self.presence_start_button.setAutoRaise(True)
        self.presence_start_button.setIcon(
            self.style().standardIcon(self.style().StandardPixmap.SP_MediaPlay)
        )
        self.presence_start_button.setStyleSheet(self._presence_button_style("#b7e4c7"))
        self.presence_stop_button = QToolButton()
        self.presence_stop_button.setToolTip("Parar presencia en Steam")
        self.presence_stop_button.setAutoRaise(True)
        self.presence_stop_button.setIcon(
            self.style().standardIcon(self.style().StandardPixmap.SP_MediaStop)
        )
        self.presence_stop_button.setStyleSheet(self._presence_button_style("#f5c2c7"))
        self.presence_restart_button = QToolButton()
        self.presence_restart_button.setToolTip("Reiniciar presencia en Steam")
        self.presence_restart_button.setAutoRaise(True)
        self.presence_restart_button.setIcon(
            self.style().standardIcon(self.style().StandardPixmap.SP_BrowserReload)
        )
        self.presence_restart_button.setStyleSheet(self._presence_button_style("#cfe8ff"))
        presence_buttons_layout.addWidget(self.presence_start_button)
        presence_buttons_layout.addWidget(self.presence_stop_button)
        presence_buttons_layout.addWidget(self.presence_restart_button)
        presence_buttons_layout.addStretch(1)
        presence_layout.addWidget(self.presence_status_label)
        presence_layout.addWidget(self.presence_detail_label)
        presence_layout.addWidget(self.presence_script_label)
        presence_layout.addWidget(self.presence_token_label)
        presence_layout.addWidget(self.presence_qr_hint_label)
        presence_layout.addWidget(self.presence_open_qr_button)
        presence_layout.addWidget(self.presence_qr_link_label)
        presence_layout.addLayout(presence_buttons_layout)
        presence_layout.addStretch(1)
        presence_group.setMaximumWidth(220)
        presence_group.setMinimumWidth(200)
        header_layout.addWidget(presence_group, stretch=1)

        actions_layout = QHBoxLayout()
        self.run_button = QPushButton("Ejecutar flujo completo")
        self.cancel_button = QPushButton("Cancelar ejecucion")
        self.cancel_button.setEnabled(False)
        self.refresh_button = QPushButton("Actualizar panel")
        actions_layout.addWidget(self.run_button)
        actions_layout.addWidget(self.cancel_button)
        actions_layout.addWidget(self.refresh_button)
        actions_layout.addStretch(1)

        self.site_table = QTableWidget(len(SITE_ORDER), 6)
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
        self.tabs.addTab(self.dashboard_tab, "Panel")

        self.history_tab = QWidget()
        history_layout = QVBoxLayout(self.history_tab)

        self.runs_table = QTableWidget(0, 10)
        self.runs_table.setHorizontalHeaderLabels(
            [
                "Fecha",
                "Hora",
                "Estado run",
                "Steam",
                "KeyDrop",
                "CSGOCases",
                "BloodyCase",
                "CS2.free",
                "G4Skins",
                "Delta",
            ]
        )
        self.runs_table.verticalHeader().setVisible(False)
        self.runs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.runs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.runs_table.setSelectionMode(QTableWidget.SingleSelection)
        self.runs_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.run_detail_label = QLabel(
            "Selecciona una ejecucion para ver el detalle completo del flujo."
        )
        self.run_detail_label.setWordWrap(True)

        self.run_detail_preview = QPlainTextEdit()
        self.run_detail_preview.setReadOnly(True)

        history_layout.addWidget(self.runs_table, stretch=1)
        history_layout.addWidget(self.run_detail_label)
        history_layout.addWidget(self.run_detail_preview, stretch=1)
        self.tabs.addTab(self.history_tab, "Historico")

        self.diagnostics_tab = QWidget()
        diagnostics_layout = QVBoxLayout(self.diagnostics_tab)

        diagnostics_filters_layout = QHBoxLayout()
        diagnostics_filters_layout.addWidget(QLabel("Sitio"))
        self.diagnostic_site_filter = QComboBox()
        self.diagnostic_site_filter.addItem("Todos")
        diagnostics_filters_layout.addWidget(self.diagnostic_site_filter)
        diagnostics_filters_layout.addWidget(QLabel("Fecha"))
        self.diagnostic_date_filter = QComboBox()
        self.diagnostic_date_filter.addItem("Todas")
        diagnostics_filters_layout.addWidget(self.diagnostic_date_filter)
        diagnostics_filters_layout.addStretch(1)

        diagnostics_actions_layout = QHBoxLayout()
        self.open_diagnostic_image_button = QPushButton("Abrir captura")
        self.open_diagnostic_text_button = QPushButton("Abrir texto")
        self.open_diagnostic_json_button = QPushButton("Abrir JSON")
        diagnostics_actions_layout.addWidget(self.open_diagnostic_image_button)
        diagnostics_actions_layout.addWidget(self.open_diagnostic_text_button)
        diagnostics_actions_layout.addWidget(self.open_diagnostic_json_button)
        diagnostics_actions_layout.addStretch(1)

        self.diagnostics_table = QTableWidget(0, 8)
        self.diagnostics_table.setHorizontalHeaderLabels(
            ["Fecha", "Sitio", "Estado", "Recompensa", "PNG", "TXT", "JSON", "Archivo"]
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

        diagnostics_layout.addLayout(diagnostics_filters_layout)
        diagnostics_layout.addLayout(diagnostics_actions_layout)
        diagnostics_layout.addWidget(self.diagnostics_table, stretch=1)
        diagnostics_layout.addWidget(self.diagnostic_files_label)
        diagnostics_layout.addWidget(self.diagnostic_preview, stretch=1)
        self.tabs.addTab(self.diagnostics_tab, "Diagnosticos")

        self.settings_tab = QWidget()
        settings_layout = QVBoxLayout(self.settings_tab)

        self.settings_summary_label = QLabel(
            "Ajusta aqui el comportamiento del flujo, Steam, la interfaz y los listados."
        )
        self.settings_summary_label.setWordWrap(True)

        settings_actions_layout = QHBoxLayout()
        self.save_settings_button = QPushButton("Guardar configuracion")
        self.reload_settings_button = QPushButton("Recargar desde disco")
        self.reset_settings_button = QPushButton("Restaurar por defecto")
        self.open_settings_file_button = QPushButton("Abrir settings.json")
        settings_actions_layout.addWidget(self.save_settings_button)
        settings_actions_layout.addWidget(self.reload_settings_button)
        settings_actions_layout.addWidget(self.reset_settings_button)
        settings_actions_layout.addWidget(self.open_settings_file_button)
        settings_actions_layout.addStretch(1)

        settings_groups_layout = QGridLayout()

        flow_group = QGroupBox("Flujo")
        flow_layout = QVBoxLayout(flow_group)
        self.enable_keydrop_checkbox = QCheckBox("Ejecutar KeyDrop")
        self.enable_csgocases_checkbox = QCheckBox("Ejecutar CSGOCases")
        self.enable_bloodycase_checkbox = QCheckBox("Ejecutar BloodyCase")
        self.enable_cs2free_checkbox = QCheckBox("Ejecutar CS2.free")
        self.enable_g4skins_checkbox = QCheckBox("Ejecutar G4Skins")
        flow_layout.addWidget(self.enable_keydrop_checkbox)
        flow_layout.addWidget(self.enable_csgocases_checkbox)
        flow_layout.addWidget(self.enable_bloodycase_checkbox)
        flow_layout.addWidget(self.enable_cs2free_checkbox)
        flow_layout.addWidget(self.enable_g4skins_checkbox)
        flow_layout.addStretch(1)

        steam_group_settings = QGroupBox("Steam")
        steam_settings_layout = QVBoxLayout(steam_group_settings)
        self.use_presence_checkbox = QCheckBox(
            "Usar Presencia en Steam durante el flujo completo"
        )
        self.headless_refresh_checkbox = QCheckBox(
            "Refrescar perfil de Steam en modo headless"
        )
        steam_settings_layout.addWidget(self.use_presence_checkbox)
        steam_settings_layout.addWidget(self.headless_refresh_checkbox)
        steam_settings_layout.addStretch(1)

        interface_group_settings = QGroupBox("Interfaz")
        interface_settings_layout = QGridLayout(interface_group_settings)
        self.initial_tab_combo = QComboBox()
        self.initial_tab_combo.addItems(TAB_OPTIONS)
        self.remember_last_tab_checkbox = QCheckBox(
            "Recordar la ultima pestaña abierta"
        )
        self.autofocus_setup_checkbox = QCheckBox(
            "Ir a Primera ejecucion si hay elementos pendientes"
        )
        interface_settings_layout.addWidget(QLabel("Pestaña inicial"), 0, 0)
        interface_settings_layout.addWidget(self.initial_tab_combo, 0, 1)
        interface_settings_layout.addWidget(self.remember_last_tab_checkbox, 1, 0, 1, 2)
        interface_settings_layout.addWidget(self.autofocus_setup_checkbox, 2, 0, 1, 2)

        data_group_settings = QGroupBox("Datos")
        data_settings_layout = QGridLayout(data_group_settings)
        self.recent_activity_limit_spin = self._make_spin_box(10, 500)
        self.runs_limit_spin = self._make_spin_box(10, 500)
        self.diagnostics_limit_spin = self._make_spin_box(10, 500)
        self.daily_totals_limit_spin = self._make_spin_box(7, 365)
        self.diagnostic_preview_chars_spin = self._make_spin_box(1000, 100000, 1000)
        data_settings_layout.addWidget(QLabel("Filas de actividad reciente"), 0, 0)
        data_settings_layout.addWidget(self.recent_activity_limit_spin, 0, 1)
        data_settings_layout.addWidget(QLabel("Filas de historico completo"), 1, 0)
        data_settings_layout.addWidget(self.runs_limit_spin, 1, 1)
        data_settings_layout.addWidget(QLabel("Filas de diagnosticos"), 2, 0)
        data_settings_layout.addWidget(self.diagnostics_limit_spin, 2, 1)
        data_settings_layout.addWidget(QLabel("Dias en acumulado diario"), 3, 0)
        data_settings_layout.addWidget(self.daily_totals_limit_spin, 3, 1)
        data_settings_layout.addWidget(QLabel("Caracteres de preview diagnostico"), 4, 0)
        data_settings_layout.addWidget(self.diagnostic_preview_chars_spin, 4, 1)

        settings_groups_layout.addWidget(flow_group, 0, 0)
        settings_groups_layout.addWidget(steam_group_settings, 0, 1)
        settings_groups_layout.addWidget(interface_group_settings, 1, 0)
        settings_groups_layout.addWidget(data_group_settings, 1, 1)
        settings_groups_layout.setColumnStretch(0, 1)
        settings_groups_layout.setColumnStretch(1, 1)

        settings_layout.addWidget(self.settings_summary_label)
        settings_layout.addLayout(settings_actions_layout)
        settings_layout.addLayout(settings_groups_layout)
        settings_layout.addStretch(1)
        self.tabs.addTab(self.settings_tab, "Configuracion")

        exit_action = QAction("Salir", self)
        exit_action.triggered.connect(self.close)
        self.addAction(exit_action)
        self.load_settings_into_controls()
        self.update_diagnostics_actions()

    def _wire_signals(self) -> None:
        self.run_button.clicked.connect(self.start_run)
        self.cancel_button.clicked.connect(self.cancel_run)
        self.refresh_button.clicked.connect(self.refresh_dashboard)
        self.refresh_setup_button.clicked.connect(self.refresh_setup_tab)
        self.prepare_all_button.clicked.connect(lambda: self.start_preparation("all"))
        self.prepare_steam_button.clicked.connect(lambda: self.start_preparation("steam"))
        self.prepare_keydrop_button.clicked.connect(lambda: self.start_preparation("keydrop"))
        self.prepare_csgocases_button.clicked.connect(lambda: self.start_preparation("csgocases"))
        self.prepare_bloodycase_button.clicked.connect(lambda: self.start_preparation("bloodycase"))
        self.prepare_cs2free_button.clicked.connect(lambda: self.start_preparation("cs2free"))
        self.prepare_g4skins_button.clicked.connect(lambda: self.start_preparation("g4skins"))
        self.revalidate_session_button.clicked.connect(self.revalidate_selected_session)
        self.delete_session_button.clicked.connect(self.delete_selected_session)
        self.session_table.itemSelectionChanged.connect(
            self.on_session_selection_changed
        )
        self.steam_refresh_button.clicked.connect(self.start_steam_refresh)
        self.presence_start_button.clicked.connect(
            lambda: self.start_presence_command("start")
        )
        self.presence_stop_button.clicked.connect(
            lambda: self.start_presence_command("stop")
        )
        self.presence_restart_button.clicked.connect(
            lambda: self.start_presence_command("restart")
        )
        self.open_sessions_dir_button.clicked.connect(
            lambda: self.open_local_path(self.paths.sessions_dir)
        )
        self.open_logs_dir_button.clicked.connect(
            lambda: self.open_local_path(self.paths.logs_dir)
        )
        self.open_data_dir_button.clicked.connect(
            lambda: self.open_local_path(self.paths.data_dir)
        )
        self.log_emitter.message.connect(self.append_log)
        self.prompt_bridge.prompt_requested.connect(self.show_prompt_dialog)
        self.runs_table.itemSelectionChanged.connect(self.on_run_selection_changed)
        self.diagnostics_table.itemSelectionChanged.connect(
            self.on_diagnostic_selection_changed
        )
        self.diagnostics_table.itemDoubleClicked.connect(
            lambda _: self.open_selected_diagnostic_file("image")
        )
        self.diagnostic_site_filter.currentIndexChanged.connect(
            lambda _: self.apply_diagnostic_filters()
        )
        self.diagnostic_date_filter.currentIndexChanged.connect(
            lambda _: self.apply_diagnostic_filters()
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
        self.presence_open_qr_button.clicked.connect(self.open_presence_qr_dialog)
        self.save_settings_button.clicked.connect(self.save_settings_from_controls)
        self.reload_settings_button.clicked.connect(self.reload_settings_from_disk)
        self.reset_settings_button.clicked.connect(self.reset_settings_to_defaults)
        self.open_settings_file_button.clicked.connect(
            lambda: self.open_local_path(self.paths.settings_file)
        )
        self.tabs.currentChanged.connect(self.on_tab_changed)
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

    def _make_spin_box(
        self,
        minimum: int,
        maximum: int,
        step: int = 1,
    ) -> QSpinBox:
        spin_box = QSpinBox()
        spin_box.setRange(minimum, maximum)
        spin_box.setSingleStep(step)
        return spin_box

    def _steam_badge_style(self, background: str, foreground: str) -> str:
        return (
            f"background-color: {background}; color: {foreground}; "
            "border-radius: 8px; padding: 4px 10px; font-size: 11px; font-weight: bold;"
        )

    def _presence_status_style(self, background: str, foreground: str) -> str:
        return (
            f"background-color: {background}; color: {foreground}; "
            "border-radius: 6px; padding: 3px 8px; font-size: 10px; font-weight: bold;"
        )

    def _presence_button_style(self, background: str) -> str:
        return (
            "QToolButton {"
            f"background-color: {background};"
            "border: 1px solid #bdbdbd;"
            "border-radius: 6px;"
            "padding: 3px;"
            "}"
            "QToolButton:hover {"
            "border-color: #7f7f7f;"
            "}"
            "QToolButton:disabled {"
            "background-color: #efefef;"
            "border-color: #d0d0d0;"
            "}"
        )

    def start_run(self) -> None:
        if self.runner_thread is not None and self.runner_thread.isRunning():
            QMessageBox.information(
                self,
                "Ejecucion en curso",
                "Ya hay una ejecucion en curso.",
            )
            return
        if self.preparation_thread is not None and self.preparation_thread.isRunning():
            QMessageBox.information(
                self,
                "Preparacion en curso",
                "Ya hay una preparacion de sesiones en curso.",
            )
            return
        if self.presence_thread is not None and self.presence_thread.isRunning():
            QMessageBox.information(
                self,
                "Accion en curso",
                "Espera a que termine la accion sobre Presencia en Steam.",
            )
            return
        if self.steam_refresh_thread is not None and self.steam_refresh_thread.isRunning():
            QMessageBox.information(
                self,
                "Actualizacion en curso",
                "Espera a que termine la actualizacion del perfil de Steam.",
            )
            return

        self.append_log("Iniciando ejecucion desde la interfaz.")
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancelar ejecucion")
        self.cancel_requested_in_ui = False
        self.current_run_progress = {
            row["site_name"]: row for row in self.build_initial_progress_state()
        }
        self.refresh_dashboard()
        self.runner_thread = RunnerThread(
            self.base_dir,
            self.prompt_bridge,
            self.log_handler,
            self.presence_service,
            self.settings,
        )
        self.runner_thread.run_finished.connect(self.on_run_finished)
        self.runner_thread.run_failed.connect(self.on_run_failed)
        self.runner_thread.progress_updated.connect(self.update_run_progress)
        self.runner_thread.steam_status_updated.connect(self.update_steam_panel)
        self.runner_thread.finished.connect(self.on_runner_thread_finished)
        self.runner_thread.start()

    def start_preparation(self, target: str) -> None:
        if self.runner_thread is not None and self.runner_thread.isRunning():
            QMessageBox.information(
                self,
                "Ejecucion en curso",
                "No se puede preparar sesiones mientras el flujo completo esta en marcha.",
            )
            return
        if self.preparation_thread is not None and self.preparation_thread.isRunning():
            QMessageBox.information(
                self,
                "Preparacion en curso",
                "Ya hay una preparacion de sesiones en curso.",
            )
            return
        if self.presence_thread is not None and self.presence_thread.isRunning():
            QMessageBox.information(
                self,
                "Accion en curso",
                "Espera a que termine la accion sobre Presencia en Steam.",
            )
            return
        if self.steam_refresh_thread is not None and self.steam_refresh_thread.isRunning():
            QMessageBox.information(
                self,
                "Actualizacion en curso",
                "Espera a que termine la actualizacion del perfil de Steam.",
            )
            return

        target_label = "todas las sesiones" if target == "all" else target
        self.append_log(f"Iniciando preparacion de {target_label} desde la interfaz.")
        self.set_preparation_buttons_enabled(False)
        self.preparation_thread = PreparationThread(
            self.base_dir,
            self.prompt_bridge,
            self.log_handler,
            target,
        )
        self.preparation_thread.preparation_finished.connect(
            self.on_preparation_finished
        )
        self.preparation_thread.preparation_failed.connect(self.on_preparation_failed)
        self.preparation_thread.steam_status_updated.connect(self.update_steam_panel)
        self.preparation_thread.finished.connect(self.on_preparation_thread_finished)
        self.preparation_thread.start()

    def start_presence_command(self, action: str) -> None:
        if self.runner_thread is not None and self.runner_thread.isRunning():
            QMessageBox.information(
                self,
                "Ejecucion en curso",
                "No se puede cambiar Presencia en Steam mientras el flujo completo esta en marcha.",
            )
            return
        if self.preparation_thread is not None and self.preparation_thread.isRunning():
            QMessageBox.information(
                self,
                "Preparacion en curso",
                "Espera a que termine la preparacion actual.",
            )
            return
        if self.presence_thread is not None and self.presence_thread.isRunning():
            QMessageBox.information(
                self,
                "Accion en curso",
                "Ya hay una accion en curso sobre Presencia en Steam.",
            )
            return
        if self.steam_refresh_thread is not None and self.steam_refresh_thread.isRunning():
            QMessageBox.information(
                self,
                "Actualizacion en curso",
                "Espera a que termine la actualizacion del perfil de Steam.",
            )
            return

        action_label = {
            "start": "inicio",
            "stop": "parada",
            "restart": "reinicio",
        }.get(action, action)
        self.append_log(f"Iniciando accion de {action_label} de Presencia en Steam.")
        self.set_presence_buttons_enabled(False)
        self.presence_thread = PresenceCommandThread(
            self.prompt_bridge,
            self.presence_service,
            action,
        )
        self.presence_thread.command_finished.connect(self.on_presence_command_finished)
        self.presence_thread.command_failed.connect(self.on_presence_command_failed)
        self.presence_thread.finished.connect(self.on_presence_command_thread_finished)
        self.presence_thread.start()

    def start_steam_refresh(self) -> None:
        if self.runner_thread is not None and self.runner_thread.isRunning():
            QMessageBox.information(
                self,
                "Ejecucion en curso",
                "No se puede refrescar Steam mientras el flujo completo esta en marcha.",
            )
            return
        if self.preparation_thread is not None and self.preparation_thread.isRunning():
            QMessageBox.information(
                self,
                "Preparacion en curso",
                "Espera a que termine la preparacion actual.",
            )
            return
        if self.presence_thread is not None and self.presence_thread.isRunning():
            QMessageBox.information(
                self,
                "Accion en curso",
                "Espera a que termine la accion sobre Presencia en Steam.",
            )
            return
        if self.steam_refresh_thread is not None and self.steam_refresh_thread.isRunning():
            QMessageBox.information(
                self,
                "Actualizacion en curso",
                "Ya hay una actualizacion del perfil de Steam en marcha.",
            )
            return

        self.append_log("Actualizando perfil de Steam desde la interfaz.")
        self.steam_refresh_button.setEnabled(False)
        self.steam_refresh_thread = SteamRefreshThread(
            self.base_dir,
            self.prompt_bridge,
            self.log_handler,
            self.get_steam_bool("headless_profile_refresh"),
        )
        self.steam_refresh_thread.refresh_finished.connect(self.on_steam_refresh_finished)
        self.steam_refresh_thread.refresh_failed.connect(self.on_steam_refresh_failed)
        self.steam_refresh_thread.steam_status_updated.connect(self.update_steam_panel)
        self.steam_refresh_thread.finished.connect(self.on_steam_refresh_thread_finished)
        self.steam_refresh_thread.start()

    def cancel_run(self) -> None:
        if self.runner_thread is None or not self.runner_thread.isRunning():
            return
        self.append_log(
            "Cancelacion solicitada desde la interfaz. El flujo se cerrara al terminar la web actual."
        )
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelando...")
        self.cancel_requested_in_ui = True
        self.refresh_dashboard()
        self.runner_thread.request_cancel()

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
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelar ejecucion")
        self.cancel_requested_in_ui = False
        self.runner_thread = None
        self.on_session_selection_changed()

    def on_preparation_finished(self, result: object) -> None:
        if isinstance(result, list):
            message = "\n".join(str(item) for item in result)
        else:
            message = str(result)
        self.append_log(f"Preparacion finalizada: {message}")
        self.refresh_dashboard()
        QMessageBox.information(self, "Preparacion completada", message)

    def on_preparation_failed(self, error_message: str) -> None:
        self.append_log(f"Error en la preparacion: {error_message}")
        QMessageBox.critical(
            self,
            "Error en la preparacion",
            error_message or "La preparacion ha fallado sin mensaje adicional.",
        )

    def on_preparation_thread_finished(self) -> None:
        self.set_preparation_buttons_enabled(True)
        self.preparation_thread = None
        self.on_session_selection_changed()

    def on_presence_command_finished(self, message: str) -> None:
        self.append_log(message)
        self.refresh_dashboard()

    def on_presence_command_failed(self, error_message: str) -> None:
        self.append_log(f"Error en Presencia en Steam: {error_message}")
        QMessageBox.critical(
            self,
            "Error en Presencia en Steam",
            error_message or "La accion sobre Presencia en Steam ha fallado.",
        )

    def on_presence_command_thread_finished(self) -> None:
        self.set_presence_buttons_enabled(True)
        self.presence_thread = None
        self.on_session_selection_changed()

    def on_steam_refresh_finished(self, message: str) -> None:
        self.append_log(message)
        self.refresh_dashboard()

    def on_steam_refresh_failed(self, error_message: str) -> None:
        self.append_log(f"Error al actualizar Steam: {error_message}")
        QMessageBox.critical(
            self,
            "Error al actualizar Steam",
            error_message or "La actualizacion del perfil de Steam ha fallado.",
        )

    def on_steam_refresh_thread_finished(self) -> None:
        self.steam_refresh_button.setEnabled(True)
        self.steam_refresh_thread = None
        self.on_session_selection_changed()

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
        self.refresh_setup_tab()
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

        daily_rows = self.history_store.get_daily_totals(
            self.get_interface_int("daily_totals_limit")
        )
        self.daily_table.setRowCount(len(daily_rows))
        for row_index, row in enumerate(daily_rows):
            self.set_table_item(self.daily_table, row_index, 0, str(row["day"]))
            self.set_table_item(
                self.daily_table,
                row_index,
                1,
                self.format_amount(row["total"]),
            )

        recent_rows = self.history_store.get_recent_site_results(
            self.get_interface_int("recent_activity_limit")
        )
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

        self.refresh_runs_table()
        self.refresh_diagnostics_table()

    def refresh_setup_tab(self) -> None:
        checks = self.build_setup_checks()
        self.refresh_session_table()
        self.setup_table.setRowCount(len(checks))

        pending_logins: list[str] = []
        blocking_issues: list[str] = []

        for row_index, check in enumerate(checks):
            category = str(check["category"])
            element = str(check["element"])
            status = str(check["status"])
            detail = str(check["detail"])

            self.set_table_item(self.setup_table, row_index, 0, category)
            self.set_table_item(self.setup_table, row_index, 1, element)
            self.set_status_table_item(self.setup_table, row_index, 2, status)
            self.set_table_item(self.setup_table, row_index, 3, detail)

            if category == "Sesion" and status != "OK":
                pending_logins.append(element)
            if category in ("Dependencia", "Recurso", "Ruta") and status == "FALTA":
                blocking_issues.append(f"{element}: {detail}")

        has_non_ok_status = any(str(check["status"]) != "OK" for check in checks)
        self.has_non_ok_setup_status = has_non_ok_status

        if blocking_issues:
            self.setup_summary_label.setText(
                "Faltan elementos antes de una primera ejecucion fiable: "
                + " | ".join(blocking_issues)
            )
        elif pending_logins:
            self.setup_summary_label.setText(
                "La app esta lista, pero algunas webs pediran login manual la primera vez."
            )
        else:
            self.setup_summary_label.setText(
                "La configuracion base parece completa. Puedes ejecutar el flujo desde la interfaz."
            )

        if pending_logins:
            self.setup_login_label.setText(
                "Webs que probablemente pediran login manual en la primera ejecucion: "
                + ", ".join(pending_logins)
                + "."
            )
        else:
            self.setup_login_label.setText(
                "Todas las sesiones detectadas tienen un archivo guardado. Si alguna web expira, la app pedira login manual solo para esa web."
            )

        if (
            has_non_ok_status
            and self.get_interface_bool("auto_focus_setup_on_issues")
            and not self.setup_autofocus_done
        ):
            self.tabs.setCurrentWidget(self.setup_tab)
            self.setup_autofocus_done = True

    def refresh_session_table(self) -> None:
        previous_row_index = self.session_table.currentRow()
        rows = self.build_session_rows()
        self.session_rows = rows
        self.session_table.setRowCount(len(rows))

        for row_index, row in enumerate(rows):
            self.set_table_item(self.session_table, row_index, 0, str(row["site_name"]))
            self.set_status_table_item(
                self.session_table,
                row_index,
                1,
                str(row["status"]),
            )
            self.set_table_item(
                self.session_table,
                row_index,
                2,
                str(row["updated_at"]),
            )
            self.set_table_item(
                self.session_table,
                row_index,
                3,
                str(row["file_name"]),
            )

        if rows:
            target_row = previous_row_index if previous_row_index >= 0 else 0
            target_row = min(target_row, len(rows) - 1)
            self.session_table.selectRow(target_row)
            self.on_session_selection_changed()
        else:
            self.session_detail_label.setText("No hay sesiones configuradas todavia.")
            self.revalidate_session_button.setEnabled(False)
            self.delete_session_button.setEnabled(False)

    def build_session_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for site_key, site_name, path in self.get_session_entries():
            check = self.make_session_check(site_name, path)
            updated_at = "-"
            if path.exists():
                try:
                    updated_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except Exception:
                    updated_at = "-"
            rows.append(
                {
                    "site_key": site_key,
                    "site_name": site_name,
                    "status": str(check["status"]),
                    "detail": str(check["detail"]),
                    "updated_at": updated_at,
                    "file_name": path.name,
                }
            )
        return rows

    def get_session_entries(self) -> list[tuple[str, str, Path]]:
        return [
            ("steam", "Steam", self.paths.steam_session_file),
            ("keydrop", "KeyDrop", self.paths.keydrop_session_file),
            ("csgocases", "CSGOCases", self.paths.csgocases_session_file),
            ("bloodycase", "BloodyCase", self.paths.bloodycase_session_file),
            ("cs2free", "CS2.free", self.paths.cs2free_session_file),
            ("g4skins", "G4Skins", self.paths.g4skins_session_file),
        ]

    def get_selected_session_row(self) -> dict[str, str] | None:
        row_index = self.session_table.currentRow()
        if row_index < 0:
            return None
        if row_index >= len(getattr(self, "session_rows", [])):
            return None
        return self.session_rows[row_index]

    def on_session_selection_changed(self) -> None:
        row = self.get_selected_session_row()
        if row is None:
            self.session_detail_label.setText(
                "Selecciona una sesion para revalidarla o borrarla."
            )
            self.revalidate_session_button.setEnabled(False)
            self.delete_session_button.setEnabled(False)
            return

        self.session_detail_label.setText(
            f"{row['site_name']}: {row['detail']}"
        )
        buttons_enabled = not self.is_any_background_task_running()
        self.revalidate_session_button.setEnabled(buttons_enabled)
        self.delete_session_button.setEnabled(buttons_enabled)

    def revalidate_selected_session(self) -> None:
        row = self.get_selected_session_row()
        if row is None:
            return
        self.start_preparation(str(row["site_key"]))

    def delete_selected_session(self) -> None:
        row = self.get_selected_session_row()
        if row is None:
            return
        site_key = str(row["site_key"])
        site_name = str(row["site_name"])
        session_path = next(
            (path for key, _, path in self.get_session_entries() if key == site_key),
            None,
        )
        if session_path is None:
            return

        answer = QMessageBox.question(
            self,
            "Borrar sesion",
            f"Se borrara la sesion guardada de {site_name}.\n\nArchivo: {session_path.name}\n\n¿Quieres continuar?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            if session_path.exists():
                session_path.unlink()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "No se pudo borrar",
                f"No se pudo borrar {session_path.name}: {exc}",
            )
            return

        self.append_log(f"Sesion borrada: {site_name}")
        self.refresh_setup_tab()

    def refresh_site_table(self, latest_site_rows: dict[str, dict[str, object]]) -> None:
        ordered_sites = list(SITE_ORDER)
        self.site_table.setRowCount(len(ordered_sites))
        for row_index, site_name in enumerate(ordered_sites):
            row = latest_site_rows.get(site_name, {})
            progress = self.current_run_progress.get(site_name, {})

            status_text = str(row.get("status", "-"))
            if progress:
                phase = str(progress.get("phase") or "Pendiente")
                result = str(progress.get("result") or "-")
                if phase == "En curso":
                    status_text = (
                        "Cancelando..."
                        if self.cancel_requested_in_ui
                        else "En curso"
                    )
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
            if self.cancel_requested_in_ui:
                status_item.setBackground(QColor("#f6c177"))
            else:
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
            elif result == "disabled":
                status_item.setBackground(QColor("#eeeeee"))
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
        if bool(steam_status.get("profile_refreshing")):
            self.steam_updated_label.setText("Actualizando...")
        else:
            self.steam_updated_label.setText(
                f"Actualizado: {self.format_timestamp(steam_status.get('profile_updated_at'))}"
            )

        self.update_presence_panel(steam_status)

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

    def update_presence_panel(self, steam_status: dict[str, object]) -> None:
        status_text = str(steam_status.get("presence_status") or "No iniciado")
        detail_text = str(steam_status.get("presence_detail") or "Servicio no arrancado")
        ready = bool(steam_status.get("presence_ready"))

        refresh_token_path = self.paths.steam_presence_script.parent / "secrets" / "refreshToken.txt"
        script_exists = self.paths.steam_presence_script.exists()
        token_exists = refresh_token_path.exists()

        self.presence_status_label.setText(status_text)
        self.presence_status_label.setStyleSheet(
            self._presence_status_style(
                self.presence_status_background(status_text, ready),
                "#111111",
            )
        )
        self.presence_detail_label.setText(detail_text)
        self.presence_script_label.setText(
            f"Script: {'OK' if script_exists else 'Falta'}"
        )
        self.presence_token_label.setText(
            f"Refresh token: {'Detectado' if token_exists else 'No detectado'}"
        )
        qr_text = str(steam_status.get("presence_qr_text") or "").strip()
        qr_url = str(steam_status.get("presence_qr_url") or "").strip()
        has_qr = bool(qr_text or qr_url)
        self.presence_qr_hint_label.setVisible(has_qr)
        self.presence_open_qr_button.setVisible(has_qr)
        self.presence_qr_link_label.setVisible(bool(qr_url))
        if qr_url:
            self.presence_qr_link_label.setText(
                f"<a href=\"{qr_url}\">{qr_url}</a>"
            )
            if qr_url != self.presence_qr_last_seen_url:
                self.presence_qr_last_seen_url = qr_url
                self.show_presence_qr_dialog(qr_url)
        else:
            self.presence_qr_link_label.clear()
            self.presence_qr_last_seen_url = None
            self.close_presence_qr_dialog()

    def build_presence_qr_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("Autorizar Presencia en Steam")
        dialog.setModal(False)
        dialog.resize(420, 520)

        layout = QVBoxLayout(dialog)
        info_label = QLabel(
            "Escanea este QR con Steam Guard para autorizar Presencia en Steam."
        )
        info_label.setWordWrap(True)

        image_label = QLabel("Generando QR...")
        image_label.setAlignment(Qt.AlignCenter)
        image_label.setMinimumHeight(320)

        link_label = QLabel("")
        link_label.setOpenExternalLinks(True)
        link_label.setWordWrap(True)
        link_label.setTextInteractionFlags(Qt.TextBrowserInteraction)

        close_button = QPushButton("Cerrar")
        close_button.clicked.connect(dialog.close)

        layout.addWidget(info_label)
        layout.addWidget(image_label, stretch=1)
        layout.addWidget(link_label)
        layout.addWidget(close_button, alignment=Qt.AlignRight)

        self.presence_qr_image_label = image_label
        self.presence_qr_dialog_link_label = link_label
        return dialog

    def show_presence_qr_dialog(self, qr_url: str) -> None:
        if not qr_url:
            return
        if self.presence_qr_dialog is None:
            self.presence_qr_dialog = self.build_presence_qr_dialog()

        pixmap = self.generate_qr_pixmap(qr_url)
        if self.presence_qr_image_label is not None:
            if pixmap is not None and not pixmap.isNull():
                self.presence_qr_image_label.setPixmap(pixmap)
                self.presence_qr_image_label.setText("")
            else:
                self.presence_qr_image_label.setPixmap(QPixmap())
                self.presence_qr_image_label.setText("No se pudo generar el QR.")
        if self.presence_qr_dialog_link_label is not None:
            self.presence_qr_dialog_link_label.setText(f"<a href=\"{qr_url}\">{qr_url}</a>")

        self.presence_qr_dialog.show()
        self.presence_qr_dialog.raise_()
        self.presence_qr_dialog.activateWindow()

    def open_presence_qr_dialog(self) -> None:
        snapshot = get_steam_status_snapshot()
        qr_url = str(snapshot.get("presence_qr_url") or "").strip()
        if not qr_url:
            QMessageBox.information(
                self,
                "QR no disponible",
                "Ahora mismo no hay ningun QR pendiente de autorizacion.",
            )
            return
        self.show_presence_qr_dialog(qr_url)

    def close_presence_qr_dialog(self) -> None:
        if self.presence_qr_dialog is None:
            return
        self.presence_qr_dialog.close()
        self.presence_qr_dialog = None
        self.presence_qr_image_label = None
        self.presence_qr_dialog_link_label = None

    def generate_qr_pixmap(self, qr_url: str) -> QPixmap | None:
        try:
            import qrcode
        except Exception:
            return None

        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(qr_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        pixmap = QPixmap()
        if not pixmap.loadFromData(buffer.getvalue(), "PNG"):
            return None
        return pixmap

    def presence_status_background(self, status_text: str, ready: bool) -> str:
        normalized = status_text.strip().lower()
        if ready or normalized == "listo":
            return "#b7e4c7"
        if normalized in {"error"}:
            return "#f5c2c7"
        if normalized in {"iniciando", "en ejecucion", "reiniciando", "no listo"}:
            return "#f4e7a3"
        return "#e9e9e9"

    def format_timestamp(self, iso_value: object) -> str:
        if not iso_value:
            return "-"
        try:
            updated_at = datetime.fromisoformat(str(iso_value))
        except Exception:
            return str(iso_value)
        return updated_at.strftime("%Y-%m-%d %H:%M:%S")

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

    def refresh_runs_table(self) -> None:
        previous_row_index = self.runs_table.currentRow()
        self.run_history_rows = self.history_store.get_recent_runs(
            self.get_interface_int("runs_limit")
        )
        self.runs_table.setRowCount(len(self.run_history_rows))

        for row_index, run_row in enumerate(self.run_history_rows):
            site_status_map = {
                str(site_row.get("site_name") or ""): str(site_row.get("status") or "-")
                for site_row in list(run_row.get("site_results") or [])
                if isinstance(site_row, dict)
            }
            started_at = str(run_row.get("started_at") or "")
            self.set_table_item(self.runs_table, row_index, 0, self.extract_date_text(started_at))
            self.set_table_item(self.runs_table, row_index, 1, self.extract_time_text(started_at))
            self.set_table_item(
                self.runs_table,
                row_index,
                2,
                str(run_row.get("run_status") or "-"),
            )
            self.set_table_item(
                self.runs_table,
                row_index,
                3,
                self.format_recent_hours(run_row.get("recent_hours")),
            )
            self.set_table_item(
                self.runs_table,
                row_index,
                4,
                site_status_map.get("keydrop", "-"),
            )
            self.set_table_item(
                self.runs_table,
                row_index,
                5,
                site_status_map.get("csgocases", "-"),
            )
            self.set_table_item(
                self.runs_table,
                row_index,
                6,
                site_status_map.get("bloodycase", "-"),
            )
            self.set_table_item(
                self.runs_table,
                row_index,
                7,
                site_status_map.get("cs2free", "-"),
            )
            self.set_table_item(
                self.runs_table,
                row_index,
                8,
                site_status_map.get("g4skins", "-"),
            )
            self.set_table_item(
                self.runs_table,
                row_index,
                9,
                self.format_amount(run_row.get("positive_delta")),
            )

        if self.run_history_rows:
            target_row = previous_row_index if previous_row_index >= 0 else 0
            target_row = min(target_row, len(self.run_history_rows) - 1)
            self.runs_table.selectRow(target_row)
            self.on_run_selection_changed()
        else:
            self.run_detail_label.setText(
                "Aun no hay ejecuciones completas registradas."
            )
            self.run_detail_preview.clear()

    def on_run_selection_changed(self) -> None:
        row_index = self.runs_table.currentRow()
        if row_index < 0 or row_index >= len(self.run_history_rows):
            self.run_detail_label.setText(
                "Selecciona una ejecucion para ver el detalle completo del flujo."
            )
            self.run_detail_preview.clear()
            return

        run_row = self.run_history_rows[row_index]
        started_at = str(run_row.get("started_at") or "-")
        finished_at = str(run_row.get("finished_at") or "-")
        self.run_detail_label.setText(
            f"Ejecucion del {started_at} | Final: {str(run_row.get('run_status') or '-')}"
        )
        self.run_detail_preview.setPlainText(self.build_run_detail_text(run_row))

    def build_run_detail_text(self, run_row: dict[str, object]) -> str:
        lines = [
            f"Inicio: {str(run_row.get('started_at') or '-')}",
            f"Fin: {str(run_row.get('finished_at') or '-')}",
            f"Estado del flujo: {str(run_row.get('run_status') or '-')}",
            f"Horas recientes de CS2: {self.format_recent_hours(run_row.get('recent_hours'))}",
            f"Saldo total detectado: {self.format_amount(run_row.get('total_balance_value'))}",
            f"Delta positivo del run: {self.format_amount(run_row.get('positive_delta'))}",
            "",
            "Sitios:",
        ]

        site_rows = list(run_row.get("site_results") or [])
        for site_row in site_rows:
            if not isinstance(site_row, dict):
                continue
            reward_text = str(site_row.get("reward_text") or "-")
            reward_kind = str(site_row.get("reward_kind") or "")
            if reward_kind and reward_kind != "unknown" and reward_text != "-":
                reward_text = f"{reward_text} [{reward_kind}]"
            lines.extend(
                [
                    f"{str(site_row.get('site_name') or '-')}: {str(site_row.get('status') or '-')}",
                    f"  Recompensa: {reward_text}",
                    f"  Saldo: {str(site_row.get('balance_text') or '-')}",
                    f"  Delta: {self.format_amount(site_row.get('balance_delta'))}",
                ]
            )

        return "\n".join(lines)

    def build_setup_checks(self) -> list[dict[str, str]]:
        checks: list[dict[str, str]] = []
        pyside_available = importlib.util.find_spec("PySide6") is not None
        playwright_available = importlib.util.find_spec("playwright") is not None
        node_available = shutil.which("node") is not None

        checks.extend(
            [
                self.make_check(
                    "Dependencia",
                    "PySide6",
                    pyside_available,
                    "Interfaz grafica disponible."
                    if pyside_available
                    else "Falta PySide6 en el entorno.",
                ),
                self.make_check(
                    "Dependencia",
                    "Playwright",
                    playwright_available,
                    "Playwright disponible."
                    if playwright_available
                    else "Falta Playwright en el entorno.",
                ),
                self.make_check(
                    "Dependencia",
                    "Node.js",
                    node_available,
                    "Node detectado para Steam Presence."
                    if node_available
                    else "No se encontro el ejecutable 'node'.",
                ),
            ]
        )

        checks.extend(
            [
                self.make_path_check("Ruta", "Carpeta sessions", self.paths.sessions_dir),
                self.make_path_check("Ruta", "Carpeta logs", self.paths.logs_dir),
                self.make_path_check("Ruta", "Carpeta data", self.paths.data_dir),
                self.make_file_check(
                    "Recurso",
                    "Steam Presence script",
                    self.paths.steam_presence_script,
                ),
                self.make_file_check(
                    "Recurso",
                    "Avatar KeyDrop",
                    self.paths.keydrop_steam_avatar_file,
                ),
                self.make_file_check(
                    "Recurso",
                    "Avatar CSGOCases",
                    self.paths.csgocases_steam_avatar_file,
                ),
                self.make_file_check(
                    "Recurso",
                    "Avatar BloodyCase",
                    self.paths.bloodycase_steam_avatar_file,
                ),
            ]
        )

        checks.extend(
            [
                self.make_session_check("Steam", self.paths.steam_session_file),
                self.make_session_check("KeyDrop", self.paths.keydrop_session_file),
                self.make_session_check("CSGOCases", self.paths.csgocases_session_file),
                self.make_session_check("BloodyCase", self.paths.bloodycase_session_file),
                self.make_session_check("CS2.free", self.paths.cs2free_session_file),
                self.make_session_check("G4Skins", self.paths.g4skins_session_file),
            ]
        )

        return checks

    def refresh_diagnostics_table(self) -> None:
        previous_row_index = self.diagnostics_table.currentRow()
        previous_site = self.diagnostic_site_filter.currentText()
        previous_date = self.diagnostic_date_filter.currentText()
        self.all_diagnostic_rows = self.history_store.get_recent_diagnostics(
            self.get_interface_int("diagnostics_limit")
        )
        self.populate_diagnostic_filters(previous_site, previous_date)
        self.apply_diagnostic_filters(previous_row_index)

    def populate_diagnostic_filters(
        self,
        previous_site: str,
        previous_date: str,
    ) -> None:
        sites = sorted(
            {
                str(row.get("site_name") or "-")
                for row in self.all_diagnostic_rows
                if row.get("site_name")
            }
        )
        dates = sorted(
            {
                self.extract_diagnostic_date(row)
                for row in self.all_diagnostic_rows
                if self.extract_diagnostic_date(row) != "-"
            },
            reverse=True,
        )

        self.diagnostic_site_filter.blockSignals(True)
        self.diagnostic_date_filter.blockSignals(True)

        self.diagnostic_site_filter.clear()
        self.diagnostic_site_filter.addItem("Todos")
        self.diagnostic_site_filter.addItems(sites)

        self.diagnostic_date_filter.clear()
        self.diagnostic_date_filter.addItem("Todas")
        self.diagnostic_date_filter.addItems(dates)

        site_index = self.diagnostic_site_filter.findText(previous_site)
        date_index = self.diagnostic_date_filter.findText(previous_date)
        self.diagnostic_site_filter.setCurrentIndex(site_index if site_index >= 0 else 0)
        self.diagnostic_date_filter.setCurrentIndex(date_index if date_index >= 0 else 0)

        self.diagnostic_site_filter.blockSignals(False)
        self.diagnostic_date_filter.blockSignals(False)

    def apply_diagnostic_filters(self, preferred_row_index: int = 0) -> None:
        selected_site = self.diagnostic_site_filter.currentText()
        selected_date = self.diagnostic_date_filter.currentText()
        self.diagnostic_rows = [
            row
            for row in self.all_diagnostic_rows
            if (selected_site in ("", "Todos") or str(row.get("site_name") or "-") == selected_site)
            and (
                selected_date in ("", "Todas")
                or self.extract_diagnostic_date(row) == selected_date
            )
        ]
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

            json_path, image_path, text_path = self.get_diagnostic_paths(row)

            self.set_table_item(
                self.diagnostics_table,
                row_index,
                0,
                self.extract_diagnostic_date(row),
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
            self.set_presence_item(
                self.diagnostics_table,
                row_index,
                4,
                image_path is not None and image_path.exists(),
            )
            self.set_presence_item(
                self.diagnostics_table,
                row_index,
                5,
                text_path is not None and text_path.exists(),
            )
            self.set_presence_item(
                self.diagnostics_table,
                row_index,
                6,
                json_path is not None and json_path.exists(),
            )
            self.set_table_item(self.diagnostics_table, row_index, 7, diagnostic_name)

        if self.diagnostic_rows:
            target_row = preferred_row_index if preferred_row_index >= 0 else 0
            target_row = min(target_row, len(self.diagnostic_rows) - 1)
            self.diagnostics_table.selectRow(target_row)
            self.on_diagnostic_selection_changed()
        else:
            if self.all_diagnostic_rows:
                self.diagnostic_files_label.setText(
                    "No hay diagnosticos que coincidan con los filtros."
                )
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

        self.diagnostic_preview.setPlainText(
            preview_text[: self.get_data_int("diagnostic_preview_chars")]
        )
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

    def extract_diagnostic_date(self, row: dict[str, object]) -> str:
        created_at = str(row.get("created_at") or "").strip()
        if len(created_at) >= 10:
            return created_at[:10]
        return created_at or "-"

    def extract_date_text(self, value: str) -> str:
        text = value.strip()
        if len(text) >= 10:
            return text[:10]
        return text or "-"

    def extract_time_text(self, value: str) -> str:
        text = value.strip()
        if len(text) >= 19:
            return text[11:19]
        return "-"

    def format_recent_hours(self, value: object) -> str:
        if value is None:
            return "-"
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"{numeric_value:.2f} h"

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

    def open_local_path(self, path: Path) -> None:
        if not path.exists():
            QMessageBox.warning(
                self,
                "Ruta no disponible",
                f"La ruta {path} no existe.",
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            QMessageBox.warning(
                self,
                "No se pudo abrir",
                f"No se pudo abrir {path}.",
            )

    def is_any_background_task_running(self) -> bool:
        return bool(
            (self.runner_thread is not None and self.runner_thread.isRunning())
            or (
                self.preparation_thread is not None
                and self.preparation_thread.isRunning()
            )
            or (self.presence_thread is not None and self.presence_thread.isRunning())
            or (
                self.steam_refresh_thread is not None
                and self.steam_refresh_thread.isRunning()
            )
        )

    def set_preparation_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.prepare_all_button,
            self.prepare_steam_button,
            self.prepare_keydrop_button,
            self.prepare_csgocases_button,
            self.prepare_bloodycase_button,
            self.prepare_cs2free_button,
            self.prepare_g4skins_button,
            self.refresh_setup_button,
        ):
            button.setEnabled(enabled)

    def set_presence_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.presence_start_button,
            self.presence_stop_button,
            self.presence_restart_button,
        ):
            button.setEnabled(enabled)

    def show_prompt_dialog(self, request: PromptRequest) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(request.title)
        dialog.setModal(True)
        dialog.resize(620, 220)

        layout = QVBoxLayout(dialog)

        message_label = QLabel(self._prompt_message_to_rich_text(request.message))
        message_label.setWordWrap(True)
        message_label.setTextFormat(Qt.TextFormat.RichText)
        message_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        message_label.setOpenExternalLinks(True)
        layout.addWidget(message_label)

        input_field = QLineEdit()
        input_field.setText(request.default)
        if request.password:
            input_field.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(input_field)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        input_field.setFocus()
        input_field.selectAll()

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.prompt_bridge.submit_answer("q")
            return
        self.prompt_bridge.submit_answer(input_field.text())

    def _prompt_message_to_rich_text(self, message: str) -> str:
        parts: list[str] = []
        last_index = 0
        for match in re.finditer(r"https?://[^\s]+", message):
            start, end = match.span()
            url = match.group(0)
            parts.append(self._html_escape(message[last_index:start]))
            escaped_url = self._html_escape(url)
            parts.append(f'<a href="{escaped_url}">{escaped_url}</a>')
            last_index = end
        parts.append(self._html_escape(message[last_index:]))
        return "".join(parts).replace("\n", "<br>")

    def _html_escape(self, value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def load_settings_into_controls(self) -> None:
        enabled_sites = set(self.get_enabled_sites())
        self.enable_keydrop_checkbox.setChecked("keydrop" in enabled_sites)
        self.enable_csgocases_checkbox.setChecked("csgocases" in enabled_sites)
        self.enable_bloodycase_checkbox.setChecked("bloodycase" in enabled_sites)
        self.enable_cs2free_checkbox.setChecked("cs2free" in enabled_sites)
        self.enable_g4skins_checkbox.setChecked("g4skins" in enabled_sites)

        self.use_presence_checkbox.setChecked(
            self.get_steam_bool("use_presence_during_run")
        )
        self.headless_refresh_checkbox.setChecked(
            self.get_steam_bool("headless_profile_refresh")
        )

        self.initial_tab_combo.setCurrentText(self.get_interface_str("initial_tab"))
        self.remember_last_tab_checkbox.setChecked(
            self.get_interface_bool("remember_last_tab")
        )
        self.autofocus_setup_checkbox.setChecked(
            self.get_interface_bool("auto_focus_setup_on_issues")
        )

        self.recent_activity_limit_spin.setValue(
            self.get_interface_int("recent_activity_limit")
        )
        self.runs_limit_spin.setValue(self.get_interface_int("runs_limit"))
        self.diagnostics_limit_spin.setValue(
            self.get_interface_int("diagnostics_limit")
        )
        self.daily_totals_limit_spin.setValue(
            self.get_interface_int("daily_totals_limit")
        )
        self.diagnostic_preview_chars_spin.setValue(
            self.get_data_int("diagnostic_preview_chars")
        )

    def collect_settings_from_controls(self) -> dict[str, object]:
        enabled_sites = [
            site_name
            for site_name, checkbox in (
                ("keydrop", self.enable_keydrop_checkbox),
                ("csgocases", self.enable_csgocases_checkbox),
                ("bloodycase", self.enable_bloodycase_checkbox),
                ("cs2free", self.enable_cs2free_checkbox),
                ("g4skins", self.enable_g4skins_checkbox),
            )
            if checkbox.isChecked()
        ]
        if not enabled_sites:
            enabled_sites = list(SITE_ORDER)

        current_width = max(self.width(), 900)
        current_height = max(self.height(), 700)
        last_tab = self.tabs.tabText(self.tabs.currentIndex()) or "Panel"

        return {
            "flow": {
                "enabled_sites": enabled_sites,
            },
            "steam": {
                "use_presence_during_run": self.use_presence_checkbox.isChecked(),
                "headless_profile_refresh": self.headless_refresh_checkbox.isChecked(),
            },
            "interface": {
                "initial_tab": self.initial_tab_combo.currentText() or "Panel",
                "auto_focus_setup_on_issues": self.autofocus_setup_checkbox.isChecked(),
                "remember_last_tab": self.remember_last_tab_checkbox.isChecked(),
                "last_tab": last_tab,
                "recent_activity_limit": self.recent_activity_limit_spin.value(),
                "runs_limit": self.runs_limit_spin.value(),
                "diagnostics_limit": self.diagnostics_limit_spin.value(),
                "daily_totals_limit": self.daily_totals_limit_spin.value(),
                "window_width": current_width,
                "window_height": current_height,
            },
            "data": {
                "diagnostic_preview_chars": self.diagnostic_preview_chars_spin.value(),
            },
        }

    def save_settings_from_controls(self) -> None:
        self.settings = self.settings_store.normalize(self.collect_settings_from_controls())
        self.settings_store.save(self.settings)
        self.load_settings_into_controls()
        self.refresh_dashboard()
        self.append_log("Configuracion guardada.")
        QMessageBox.information(
            self,
            "Configuracion guardada",
            "La configuracion se ha guardado y ya esta aplicada.",
        )

    def reload_settings_from_disk(self) -> None:
        self.settings = self.settings_store.load()
        self.load_settings_into_controls()
        self.resize(
            self.get_interface_int("window_width"),
            self.get_interface_int("window_height"),
        )
        self.refresh_dashboard()
        self.append_log("Configuracion recargada desde disco.")

    def reset_settings_to_defaults(self) -> None:
        self.settings = self.settings_store.reset()
        self.load_settings_into_controls()
        self.resize(
            self.get_interface_int("window_width"),
            self.get_interface_int("window_height"),
        )
        self.refresh_dashboard()
        self.append_log("Configuracion restaurada a valores por defecto.")
        QMessageBox.information(
            self,
            "Configuracion restaurada",
            "Se han restaurado los valores por defecto.",
        )

    def build_initial_progress_state(self) -> list[dict[str, str]]:
        enabled_sites = set(self.get_enabled_sites())
        rows: list[dict[str, str]] = []
        for site_name in SITE_ORDER:
            if site_name in enabled_sites:
                rows.append({"site_name": site_name, "phase": "Pendiente", "result": "-"})
            else:
                rows.append(
                    {"site_name": site_name, "phase": "Hecho", "result": "disabled"}
                )
        return rows

    def get_enabled_sites(self) -> list[str]:
        flow_settings = self.settings.get("flow")
        if not isinstance(flow_settings, dict):
            return list(SITE_ORDER)
        enabled_sites = flow_settings.get("enabled_sites")
        if not isinstance(enabled_sites, list):
            return list(SITE_ORDER)
        normalized = [str(site).strip().lower() for site in enabled_sites]
        selected = [site for site in SITE_ORDER if site in normalized]
        return selected or list(SITE_ORDER)

    def get_steam_bool(self, key: str) -> bool:
        steam_settings = self.settings.get("steam")
        if not isinstance(steam_settings, dict):
            return False
        return bool(steam_settings.get(key))

    def get_interface_bool(self, key: str) -> bool:
        interface_settings = self.settings.get("interface")
        if not isinstance(interface_settings, dict):
            return False
        return bool(interface_settings.get(key))

    def get_interface_int(self, key: str) -> int:
        interface_settings = self.settings.get("interface")
        if not isinstance(interface_settings, dict):
            return 0
        value = interface_settings.get(key)
        return int(value) if isinstance(value, int) else 0

    def get_interface_str(self, key: str) -> str:
        interface_settings = self.settings.get("interface")
        if not isinstance(interface_settings, dict):
            return ""
        value = interface_settings.get(key)
        return str(value) if value is not None else ""

    def get_data_int(self, key: str) -> int:
        data_settings = self.settings.get("data")
        if not isinstance(data_settings, dict):
            return 0
        value = data_settings.get(key)
        return int(value) if isinstance(value, int) else 0

    def get_tab_widget_by_name(self, tab_name: str) -> QWidget:
        mapping = {
            "Panel": self.dashboard_tab,
            "Primera ejecucion": self.setup_tab,
            "Historico": self.history_tab,
            "Diagnosticos": self.diagnostics_tab,
            "Configuracion": self.settings_tab,
        }
        return mapping.get(tab_name, self.dashboard_tab)

    def apply_initial_tab_preference(self) -> None:
        if self.has_non_ok_setup_status and self.get_interface_bool(
            "auto_focus_setup_on_issues"
        ):
            self.tabs.setCurrentWidget(self.setup_tab)
            return

        if self.get_interface_bool("remember_last_tab"):
            tab_name = self.get_interface_str("last_tab") or "Panel"
        else:
            tab_name = self.get_interface_str("initial_tab") or "Panel"
        self.tabs.setCurrentWidget(self.get_tab_widget_by_name(tab_name))

    def on_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        if not self.get_interface_bool("remember_last_tab"):
            return
        current_tab = self.tabs.tabText(index)
        interface_settings = dict(self.settings.get("interface") or {})
        interface_settings["last_tab"] = current_tab
        self.settings["interface"] = interface_settings
        self.settings_store.save(self.settings)

    def closeEvent(self, event) -> None:
        interface_settings = dict(self.settings.get("interface") or {})
        interface_settings["window_width"] = max(self.width(), 900)
        interface_settings["window_height"] = max(self.height(), 700)
        interface_settings["last_tab"] = self.tabs.tabText(self.tabs.currentIndex()) or "Panel"
        self.settings["interface"] = interface_settings
        self.settings_store.save(self.settings)
        super().closeEvent(event)

    def set_table_item(
        self,
        table: QTableWidget,
        row: int,
        column: int,
        value: str,
    ) -> None:
        table.setItem(row, column, QTableWidgetItem(value))

    def set_status_table_item(
        self,
        table: QTableWidget,
        row: int,
        column: int,
        value: str,
    ) -> None:
        item = QTableWidgetItem(value)
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QColor("#111111"))
        if value == "OK":
            item.setBackground(QColor("#b7e4c7"))
        elif value == "PENDIENTE":
            item.setBackground(QColor("#f4e7a3"))
        else:
            item.setBackground(QColor("#f5c2c7"))
        table.setItem(row, column, item)

    def set_presence_item(
        self,
        table: QTableWidget,
        row: int,
        column: int,
        present: bool,
    ) -> None:
        item = QTableWidgetItem("Si" if present else "No")
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QColor("#111111"))
        item.setBackground(QColor("#b7e4c7") if present else QColor("#f5c2c7"))
        table.setItem(row, column, item)

    def make_check(
        self,
        category: str,
        element: str,
        ok: bool,
        detail: str,
    ) -> dict[str, str]:
        return {
            "category": category,
            "element": element,
            "status": "OK" if ok else "FALTA",
            "detail": detail,
        }

    def make_path_check(self, category: str, element: str, path: Path) -> dict[str, str]:
        return self.make_check(
            category,
            element,
            path.exists() and path.is_dir(),
            str(path),
        )

    def make_file_check(self, category: str, element: str, path: Path) -> dict[str, str]:
        return self.make_check(
            category,
            element,
            path.exists() and path.is_file(),
            str(path),
        )

    def make_session_check(self, site_name: str, path: Path) -> dict[str, str]:
        if not path.exists():
            return {
                "category": "Sesion",
                "element": site_name,
                "status": "PENDIENTE",
                "detail": f"No existe sesion previa en {path.name}. La app pedira login manual la primera vez.",
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "category": "Sesion",
                "element": site_name,
                "status": "FALTA",
                "detail": f"El archivo {path.name} existe pero no se pudo leer correctamente.",
            }

        if isinstance(payload, dict) and payload:
            return {
                "category": "Sesion",
                "element": site_name,
                "status": "OK",
                "detail": f"Sesion detectada en {path.name}.",
            }

        return {
            "category": "Sesion",
            "element": site_name,
            "status": "PENDIENTE",
            "detail": f"El archivo {path.name} esta vacio o no contiene datos utiles. La app pedira login manual.",
        }

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
    base_font = QFont()
    base_font.setFamilies(
        [
            "Noto Sans Sinhala",
            "Noto Sans",
            "DejaVu Sans",
            "Sans Serif",
        ]
    )
    app.setFont(base_font)
    window = DashboardWindow(base_dir)
    window.show()
    return app.exec()
