# Starts all four services in separate windows. Run from the shopsense folder: .\scripts\run_all.ps1
$services = @(
    @{ module = "services.inventory.main:app"; port = 8002 },
    @{ module = "services.orders.main:app";    port = 8001 },
    @{ module = "services.agents.main:app";    port = 8003 },
    @{ module = "services.gateway.main:app";   port = 8000 }
)
foreach ($s in $services) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command",
        ".\.venv\Scripts\Activate.ps1; uvicorn $($s.module) --port $($s.port)"
}
