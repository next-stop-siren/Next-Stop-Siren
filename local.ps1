$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($env:B71_PYTHON) {
    & $env:B71_PYTHON scripts/local.py @args
} else {
    & py -3.13 scripts/local.py @args
}
exit $LASTEXITCODE
