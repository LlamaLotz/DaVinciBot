$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    python -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
    python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Lint failed" }
    python -m build --wheel
    if ($LASTEXITCODE -ne 0) { throw "Wheel build failed" }
    pyinstaller --noconfirm packaging/davincibot.spec
    if ($LASTEXITCODE -ne 0) { throw "Executable build failed" }
    Write-Host "Built wheel and dist/DaVinciBot.exe"
} finally {
    Pop-Location
}
