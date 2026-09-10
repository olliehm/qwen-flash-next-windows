# Launcher for the Lemonade admission shim.
#
#   .\start.ps1          run the shim in the foreground
#   .\start.ps1 -Setup   create/repair the venv, then run
#
# The shim fronts all configured Lemonade planes on :13304.
#
# Note: no 2>&1 redirection on native exes anywhere in here. Under PowerShell 5.1
# that wraps stderr in ErrorRecords and trips ErrorActionPreference even on exit 0.

param([switch]$Setup)

$ErrorActionPreference = "Stop"

$VenvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Shim   = Join-Path $PSScriptRoot "admission_shim.py"
$Reqs   = Join-Path $PSScriptRoot "requirements.txt"
$Config = Join-Path $PSScriptRoot "footprint_table.json"

if (-not (Test-Path $Shim)) {
    Write-Error "admission_shim.py not found at $Shim."
    exit 1
}
if (-not (Test-Path $Config)) {
    Write-Warning "No footprint_table.json - copy footprint_table.example.json and edit it. Running as a plain passthrough proxy."
}

if ($Setup -or -not (Test-Path $VenvPy)) {
    if (-not (Test-Path $VenvPy)) {
        Write-Host "venv missing - creating it..."
    }
    $SysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $SysPy) { $SysPy = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $SysPy) {
        Write-Error "No python on PATH. Install Python 3.12+ and retry."
        exit 1
    }
    & $SysPy -m venv (Join-Path $PSScriptRoot ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed."; exit 1 }

    if (Test-Path $Reqs) {
        & $VenvPy -m pip install --quiet -r $Reqs
    } else {
        & $VenvPy -m pip install --quiet fastapi "uvicorn[standard]" httpx
    }
    if ($LASTEXITCODE -ne 0) { Write-Error "dependency install failed."; exit 1 }
    Write-Host "venv ready at $VenvPy"
}

Write-Host "Starting admission shim on :13304..."
& $VenvPy $Shim
