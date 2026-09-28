$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($env:B71_PYTHON) {
    & $env:B71_PYTHON scripts/windows_check.py
} else {
    & py -3.13 scripts/windows_check.py
}
exit $LASTEXITCODE
