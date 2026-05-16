from __future__ import annotations

import argparse
import json
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
from steam_status import configure_steam_status_store, set_steam_status_callback

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

    ensure_steam_presence_npm_dependencies(BASE_DIR)
    run_gui()


def run_cli() -> None:
    reset_interaction_provider()
    paths = RuntimePaths.from_base_dir(BASE_DIR)
    ensure_runtime_dirs(paths)
    configure_steam_status_store(paths.steam_state_file)
    set_steam_status_callback(None)
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


def ensure_steam_presence_npm_dependencies(base_dir: Path) -> None:
    package_json_path = base_dir / "package.json"
    if not package_json_path.exists():
        return

    try:
        package_payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except Exception:
        print(
            "No se pudo leer package.json para comprobar las dependencias de Steam Presence.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    dependencies = package_payload.get("dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        return

    node_modules_dir = base_dir / "node_modules"
    missing_packages: list[str] = []
    for package_name in dependencies.keys():
        package_path = node_modules_dir.joinpath(*str(package_name).split("/"))
        if not package_path.exists():
            missing_packages.append(str(package_name))

    if not missing_packages:
        return

    print(
        "Faltan dependencias npm de Steam Presence. Ejecuta 'npm install' en este directorio antes de abrir la interfaz.",
        file=sys.stderr,
    )
    print(
        "Paquetes no detectados: " + ", ".join(missing_packages),
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
