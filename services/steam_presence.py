from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from interaction import ask_text
from steam_status import reset_steam_presence_boot_state, update_steam_presence


@dataclass(slots=True)
class SteamPresenceService:
    script_path: Path
    logger: logging.Logger
    restart_delay_seconds: float = 3.0
    process: subprocess.Popen[str] | None = field(default=None, init=False)
    supervisor_thread: threading.Thread | None = field(default=None, init=False)
    stdout_thread: threading.Thread | None = field(default=None, init=False)
    stderr_thread: threading.Thread | None = field(default=None, init=False)
    stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    permanent_failure_detected: threading.Event = field(
        default_factory=threading.Event, init=False
    )
    input_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    ready_event: threading.Event = field(default_factory=threading.Event, init=False)
    qr_lines: list[str] = field(default_factory=list, init=False)
    qr_capture_active: bool = field(default=False, init=False)

    def start(self) -> None:
        if self.supervisor_thread is not None and self.supervisor_thread.is_alive():
            self.logger.info("El servicio auxiliar de presencia Steam ya estaba iniciado.")
            return

        self.stop_event.clear()
        self.permanent_failure_detected.clear()
        self.ready_event.clear()
        self.qr_lines.clear()
        self.qr_capture_active = False
        update_steam_presence(
            "Iniciando",
            f"Preparando {self.script_path.name}",
            ready=False,
            qr_text=None,
            qr_url=None,
        )
        self.supervisor_thread = threading.Thread(
            target=self._supervise,
            name="steam-presence-supervisor",
            daemon=True,
        )
        self.supervisor_thread.start()
        self.logger.info("Supervisor del servicio auxiliar de presencia Steam iniciado.")

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self) -> None:
        self.stop_event.set()

        process = self.process
        if process is not None and process.poll() is None:
            self.logger.info("Deteniendo servicio auxiliar de presencia Steam.")
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.logger.warning(
                    "El servicio auxiliar de presencia Steam no cerro a tiempo. Se fuerza cierre."
                )
                process.kill()
                process.wait(timeout=5)
            except KeyboardInterrupt:
                self.logger.info(
                    "Interrupcion durante la espera de cierre de Steam Presence. Se continua con el cierre."
                )
                return

        self.process = None

        if self.supervisor_thread is not None and self.supervisor_thread.is_alive():
            try:
                self.supervisor_thread.join(timeout=2)
            except KeyboardInterrupt:
                self.logger.info(
                    "Interrupcion durante el cierre del supervisor de Steam Presence."
                )
                return
        self.supervisor_thread = None
        reset_steam_presence_boot_state(token_detected=self._refresh_token_file().exists())

    def is_running(self) -> bool:
        process = self.process
        if process is not None and process.poll() is None:
            return True
        thread = self.supervisor_thread
        return thread is not None and thread.is_alive()

    def wait_until_ready(self, timeout_seconds: float = 180.0) -> bool:
        if self._refresh_token_file().exists():
            update_steam_presence(
                "Listo",
                "Refresh token detectado",
                ready=True,
                qr_text=None,
                qr_url=None,
            )
            return True

        started_at = time.monotonic()
        while time.monotonic() - started_at < timeout_seconds:
            if self.ready_event.wait(timeout=0.25):
                update_steam_presence(
                    "Listo",
                    "Steam Presence conectado",
                    ready=True,
                    qr_text=None,
                    qr_url=None,
                )
                return True
            if self.permanent_failure_detected.is_set():
                update_steam_presence(
                    "Error",
                    "Error permanente en Steam Presence",
                    ready=False,
                    qr_text=None,
                    qr_url=None,
                )
                return False
            process = self.process
            if process is not None and process.poll() is not None:
                ready = self._refresh_token_file().exists()
                update_steam_presence(
                    "Listo" if ready else "Detenido",
                    "Refresh token detectado" if ready else "Servicio no disponible",
                    ready=ready,
                    qr_text=None,
                    qr_url=None,
                )
                return ready

        ready = self._refresh_token_file().exists()
        update_steam_presence(
            "Listo" if ready else "No listo",
            "Refresh token detectado" if ready else "Esperando conexion",
            ready=ready,
            qr_text=None,
            qr_url=None,
        )
        return ready

    def _supervise(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._start_process()
            except Exception:
                self.logger.exception("No se pudo iniciar el servicio auxiliar de presencia Steam.")
                return

            process = self.process
            if process is None:
                return

            exit_code = process.wait()
            if self.stop_event.is_set():
                self.logger.info("Servicio auxiliar de presencia Steam detenido.")
                reset_steam_presence_boot_state(
                    token_detected=self._refresh_token_file().exists()
                )
                return
            if self.permanent_failure_detected.is_set():
                self.logger.warning(
                    "El servicio auxiliar de presencia Steam no se reiniciara automaticamente "
                    "hasta corregir el problema actual."
                )
                update_steam_presence(
                    "Error",
                    "Configuracion o autenticacion invalida",
                    ready=False,
                    qr_text=None,
                    qr_url=None,
                )
                return

            self.logger.warning(
                "El servicio auxiliar de presencia Steam finalizo con codigo %s. Reintentando en %.1f s.",
                exit_code,
                self.restart_delay_seconds,
            )
            update_steam_presence(
                "Reiniciando",
                f"Ultimo codigo de salida: {exit_code}",
                ready=False,
                qr_text=None,
                qr_url=None,
            )
            time.sleep(self.restart_delay_seconds)

    def _start_process(self) -> None:
        node_binary = shutil.which("node")
        if node_binary is None:
            raise RuntimeError("No se encontro 'node' en PATH.")
        if not self.script_path.exists():
            raise FileNotFoundError(f"No existe el script auxiliar: {self.script_path}")

        working_dir = self.script_path.parent
        env = os.environ.copy()

        self.process = subprocess.Popen(
            [node_binary, str(self.script_path.name)],
            cwd=str(working_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.logger.info(
            "Servicio auxiliar de presencia Steam iniciado con PID %s.",
            self.process.pid,
        )
        update_steam_presence(
            "En ejecucion",
            f"PID {self.process.pid}",
            ready=False,
            qr_text=None,
            qr_url=None,
        )

        assert self.process.stdout is not None
        assert self.process.stderr is not None

        self.stdout_thread = threading.Thread(
            target=self._stream_output,
            args=(self.process.stdout, logging.INFO, "stdout"),
            name="steam-presence-stdout",
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._stream_output,
            args=(self.process.stderr, logging.WARNING, "stderr"),
            name="steam-presence-stderr",
            daemon=True,
        )
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _stream_output(self, stream: TextIO, level: int, stream_name: str) -> None:
        try:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                if self._is_steam_guard_prompt(line):
                    self.logger.info("[steam-presence/%s] %s", stream_name, line)
                    self._provide_steam_guard_code(line)
                    continue
                if self._is_qr_start_line(line):
                    self.qr_capture_active = True
                    self.qr_lines = [line]
                    update_steam_presence(
                        "Escanea QR",
                        "Escanea el QR desde la interfaz con Steam Guard",
                        ready=False,
                        qr_text=line,
                        qr_url=None,
                    )
                    self.logger.log(level, "[steam-presence/%s] %s", stream_name, line)
                    continue
                if self.qr_capture_active:
                    self._consume_qr_line(line, level, stream_name)
                    continue
                if self._is_ready_signal(line):
                    self.ready_event.set()
                    update_steam_presence(
                        "Listo",
                        "Steam Presence conectado",
                        ready=True,
                        qr_text=None,
                        qr_url=None,
                    )
                if self._should_suppress_output(line):
                    continue
                if self._is_permanent_config_error(line):
                    self.permanent_failure_detected.set()
                    update_steam_presence(
                        "Error",
                        line,
                        ready=False,
                        qr_text=None,
                        qr_url=None,
                    )
                self.logger.log(level, "[steam-presence/%s] %s", stream_name, line)
        finally:
            stream.close()

    def _provide_steam_guard_code(self, line: str) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            self.logger.warning(
                "Se pidio codigo de Steam Guard pero el proceso auxiliar ya no esta disponible."
            )
            return

        guard_type = line.split(":", 1)[1] if ":" in line else "Steam Guard"

        with self.input_lock:
            code = ask_text(
                f"Introduce el codigo 2FA de {guard_type} para Steam Presence: ",
                title="Steam Guard requerido",
            ).strip()

        if not code:
            self.permanent_failure_detected.set()
            self.logger.warning(
                "No se introdujo codigo 2FA para Steam Presence. El servicio auxiliar no se reiniciara."
            )
            update_steam_presence("Error", "Codigo 2FA no introducido", ready=False)
            process.terminate()
            return

        process.stdin.write(code + "\n")
        process.stdin.flush()

    def _is_ready_signal(self, line: str) -> bool:
        normalized = line.lower()
        return "refresh token guardado" in normalized or "conectado" in normalized

    def _should_suppress_output(self, line: str) -> bool:
        normalized = line.lower()
        return normalized == "conectado" or normalized.startswith("conectado |")

    def _is_qr_start_line(self, line: str) -> bool:
        normalized = line.lower()
        return "escanea este qr" in normalized

    def _consume_qr_line(self, line: str, level: int, stream_name: str) -> None:
        self.qr_lines.append(line)
        qr_text = "\n".join(self.qr_lines)
        qr_url = None
        if line.startswith("https://s.team/q/"):
            qr_url = line
            self.qr_capture_active = False
        update_steam_presence(
            "Escanea QR",
            "Escanea el QR desde la interfaz con Steam Guard",
            ready=False,
            qr_text=qr_text,
            qr_url=qr_url,
        )
        self.logger.log(level, "[steam-presence/%s] %s", stream_name, line)

    def _is_steam_guard_prompt(self, line: str) -> bool:
        return line.startswith("STEAM_GUARD_CODE_REQUIRED:")

    def _refresh_token_file(self) -> Path:
        return self.script_path.parent / "secrets" / "refreshToken.txt"

    def _is_permanent_config_error(self, line: str) -> bool:
        normalized = line.lower()
        permanent_markers = (
            "se ha generado",
            "rellena accountname y password",
            "debe contener accountname y password",
            "invalidpassword",
            "steam guard solicitado de nuevo",
            "ratelimitexceeded",
        )
        return any(marker in normalized for marker in permanent_markers)
