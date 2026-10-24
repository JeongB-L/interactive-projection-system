<#
 One-command setup for Windows: installs the C++ toolchain (MSYS2), builds the hub, runs the tests.
 Run from the repo folder:   powershell -ExecutionPolicy Bypass -File setup.ps1
 Safe to run again; it skips what is already installed. Nothing here costs money or needs an account.
#>
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Say($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

Say "Checking Python"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "Python 3.8+ is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH'), then rerun." }
& python --version

Say "Checking MSYS2 (the C++ toolchain)"
$msys = 'C:\msys64'
if (-not (Test-Path "$msys\usr\bin\bash.exe")) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "MSYS2 not found; installing it with winget..."
        winget install -e --id MSYS2.MSYS2 --accept-source-agreements --accept-package-agreements
    }
    if (-not (Test-Path "$msys\usr\bin\bash.exe")) {
        throw "MSYS2 is not installed at C:\msys64. Install it from https://www.msys2.org/ (default folder), then rerun."
    }
}

Say "Installing compiler, CMake, Boost, OpenSSL, SQLite (pacman)"
$pk = 'mingw-w64-ucrt-x86_64-gcc mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-ninja mingw-w64-ucrt-x86_64-boost mingw-w64-ucrt-x86_64-openssl mingw-w64-ucrt-x86_64-sqlite3 mingw-w64-ucrt-x86_64-nlohmann-json'
& "$msys\usr\bin\bash.exe" -lc "pacman -S --noconfirm --needed $pk"
if ($LASTEXITCODE -ne 0) { throw "pacman failed (see output above)" }

Say "Creating .env (secrets file) if missing"
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "created .env  (add ANTHROPIC_API_KEY there only if you want the AI app)" }
else { Write-Host ".env already exists, left alone" }

Say "Building the hub"
& python ips.py build
if ($LASTEXITCODE -ne 0) { throw "build failed" }

Say "Running unit tests"
& python ips.py test
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

Write-Host "`nSetup complete. Try it:" -ForegroundColor Green
Write-Host "  python ips.py start`n  python ips.py open`n  python ips.py sim`n  python ips.py stop"
Write-Host "Real Pi + cameras: see docs/RUNBOOK.md ('Real Pi')."
