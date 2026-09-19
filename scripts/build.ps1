$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    python -m pytest
    python -m ruff check .
    python -m build --wheel
    pyinstaller --noconfirm packaging/davincibot.spec
    Write-Host "Built wheel and dist/DaVinciBot.exe"
} finally {
    Pop-Location
}
