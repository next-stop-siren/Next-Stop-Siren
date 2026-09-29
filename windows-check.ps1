$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
try {
    & (Join-Path $PSScriptRoot 'bootstrap.ps1')
} catch {
    # Python may not exist yet. Publish a fresh, bounded result without exception text.
    $template = Join-Path $PSScriptRoot 'scripts/windows-bootstrap-failure.json'
    $reportDir = Join-Path $PSScriptRoot '.cache/windows-check'
    New-Item -ItemType Directory -Force $reportDir | Out-Null
    $report = Join-Path $reportDir 'report.json'
    $temporary = Join-Path $reportDir ('report.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllText($temporary, (Get-Content -LiteralPath $template -Raw),
                                        [System.Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $report) {
            [System.IO.File]::Replace($temporary, $report, $null)
        } else {
            [System.IO.File]::Move($temporary, $report)
        }
    } finally {
        if (Test-Path $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
    [Console]::Error.WriteLine('Host tool preparation failed. See .cache/windows-check/report.json; check network and pinned tool artifacts.')
    exit 2
}
$tools = Join-Path $PSScriptRoot '.cache/host-tools'
$nodeDir = Join-Path $tools 'node-24.21.0-win-x64'
if (-not $env:B71_NODE -and (Test-Path (Join-Path $nodeDir 'node.exe'))) { $env:B71_NODE = Join-Path $nodeDir 'node.exe' }
if (-not $env:B71_NPM -and $env:B71_NODE -and (Test-Path (Join-Path (Split-Path $env:B71_NODE) 'npm.cmd'))) {
    $env:B71_NPM = Join-Path (Split-Path $env:B71_NODE) 'npm.cmd'
}
if (-not $env:B71_PYTHON -and (Test-Path (Join-Path $tools 'python-3.13.15-win-x64/python/python.exe'))) {
    $env:B71_PYTHON = Join-Path $tools 'python-3.13.15-win-x64/python/python.exe'
}
if (-not $env:B71_UV -and (Test-Path (Join-Path $tools 'uv-0.11.19-win-x64/uv.exe'))) { $env:B71_UV = Join-Path $tools 'uv-0.11.19-win-x64/uv.exe' }
if ($env:B71_NODE) { $env:PATH = "$(Split-Path $env:B71_NODE);$env:PATH" }
if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $tools 'uv-cache' }
if ($env:B71_PYTHON) {
    & $env:B71_PYTHON scripts/windows_check.py
} else {
    & py -3.13 scripts/windows_check.py
}
exit $LASTEXITCODE
