#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

require_cmd() {
  local cmd="$1"
  local hint="$2"

  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Falta el comando requerido: $cmd"
    echo "$hint"
    exit 1
  fi
}

echo "Instalando requisitos de daily_cases_bot..."

require_cmd python3 "Instala Python 3 y vuelve a ejecutar este script."
require_cmd npm "Instala Node.js y npm y vuelve a ejecutar este script."

cd "$ROOT_DIR"

echo
echo "[1/3] Instalando dependencias Node.js con npm..."
npm install

echo
echo "[2/3] Instalando dependencias Python..."
python3 -m pip install -r requirements.txt

echo
echo "[3/3] Instalando Chromium para Playwright..."
python3 -m playwright install chromium

echo
echo "Instalacion completada."
echo "Ya puedes ejecutar: python3 main.py"
