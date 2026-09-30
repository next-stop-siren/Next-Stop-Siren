$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($args.Count -gt 0 -and $args[0] -eq 'setup') { & (Join-Path $PSScriptRoot 'bootstrap.ps1') }
$tools = Join-Path $PSScriptRoot '.cache/host-tools'
$nodeDir = Join-Path $tools 'node-24.21.0-win-x64'
if (-not $env:B71_NODE -and (Test-Path (Join-Path $nodeDir 'node.exe'))) { $env:B71_NODE = Join-Path $nodeDir 'node.exe' }
if (-not $env:B71_NPM -and $env:B71_NODE -and (Test-Path (Join-Path (Split-Path $env:B71_NODE) 'npm.cmd'))) {
    $env:B71_NPM = Join-Path (Split-Path $env:B71_NODE) 'npm.cmd'
}
if (-not $env:B71_PYTHON -and (Test-Path (Join-Path $tools 'python-3.13.15-win-x64/python/python.exe'))) {
    $env:B71_PYTHON = Join-Path $tools 'python-3.13.15-win-x64/python/python.exe'
}
if (-not $env:B71_UV -and (Test-Path (Join-Path $tools 'uv-0.11.19-win-x64/uv.exe'))) {
    $env:B71_UV = Join-Path $tools 'uv-0.11.19-win-x64/uv.exe'
}
if ($env:B71_NODE) { $env:PATH = "$(Split-Path $env:B71_NODE);$env:PATH" }
if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $tools 'uv-cache' }
if ($env:B71_PYTHON) {
    & $env:B71_PYTHON scripts/local.py @args
} else {
    & py -3.13 scripts/local.py @args
}
exit $LASTEXITCODE
