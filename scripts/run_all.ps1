# Starts the four services, each in its own window (close a window to stop that service).
#
# How to run it:
#   PowerShell:                 .\scripts\run_all.ps1
#   cmd.exe, Git Bash, or a double-click in Explorer:   scripts\run_all.cmd
# (A bare .ps1 file only RUNS inside PowerShell; anywhere else Windows opens it in Notepad. The .cmd file
#  is a one-line launcher that works from every shell.)
#
# This script works from ANY folder: it finds the project itself, and it calls the virtual environment's
# Python directly, so you do not need to activate the environment first.

$root = Split-Path -Parent $PSScriptRoot                    # the shopsense folder (the parent of scripts\)
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Could not find $python" -ForegroundColor Red
    Write-Host "Create the virtual environment first:  python -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# Inventory first: orders depends on it.
$services = @(
    @{ name = "inventory"; module = "services.inventory.main:app"; port = 8002 },
    @{ name = "orders";    module = "services.orders.main:app";    port = 8001 },
    @{ name = "agents";    module = "services.agents.main:app";    port = 8003 },
    @{ name = "gateway";   module = "services.gateway.main:app";   port = 8000 }
)

foreach ($s in $services) {
    # Each window: label it, move to the project folder, start the service.
    $command = "`$Host.UI.RawUI.WindowTitle = 'shopsense $($s.name) :$($s.port)'; " +
               "Set-Location '$root'; " +
               "& '$python' -m uvicorn $($s.module) --port $($s.port)"
    Start-Process powershell -ArgumentList "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command
}

Write-Host "Started 4 service windows. Give them about 10 seconds, then open http://127.0.0.1:8000/" -ForegroundColor Green
