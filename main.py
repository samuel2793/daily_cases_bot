from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import (
    DailyCasesRunner,
    HistoryStore,
    RuntimePaths,
    configure_logging,
    ensure_runtime_dirs,
)
from interaction import reset_interaction_provider

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Ejecuta el flujo en modo consola en lugar de abrir la interfaz.",
    )
    args = parser.parse_args()

    if args.cli:
        run_cli()
        return

    run_gui()


def run_cli() -> None:
    reset_interaction_provider()
    paths = RuntimePaths.from_base_dir(BASE_DIR)
    ensure_runtime_dirs(paths)
    logger = configure_logging(paths.log_file)
    history_store = HistoryStore(paths.db_file)
    history_store.initialize()
    runner = DailyCasesRunner(paths, logger, history_store=history_store)
    runner.run()


def run_gui() -> None:
    try:
        from ui import launch_app
    except ModuleNotFoundError as exc:
        print(
            "No se pudo iniciar la interfaz porque falta una dependencia gráfica. "
            "Instala requirements.txt o ejecuta 'python3 main.py --cli'.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    raise SystemExit(launch_app(BASE_DIR))


if __name__ == "__main__":
    main()
