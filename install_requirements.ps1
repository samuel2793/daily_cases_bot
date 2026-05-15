$ErrorActionPreference = "Stop"

function Require-Command {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Hint
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Host "Falta el comando requerido: $Name"
        Write-Host $Hint
        exit 1
    }
}

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir

Write-Host "Instalando requisitos de daily_cases_bot..."

Require-Command -Name "python" -Hint "Instala Python 3 y vuelve a ejecutar este script."
Require-Command -Name "npm" -Hint "Instala Node.js y npm y vuelve a ejecutar este script."

Write-Host ""
Write-Host "[1/3] Instalando dependencias Node.js con npm..."
npm install

Write-Host ""
Write-Host "[2/3] Instalando dependencias Python..."
python -m pip install -r requirements.txt

Write-Host ""
Write-Host "[3/3] Instalando Chromium para Playwright..."
python -m playwright install chromium

Write-Host ""
Write-Host "Instalacion completada."
Write-Host "Ya puedes ejecutar: python main.py"
