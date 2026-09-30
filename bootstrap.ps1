$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$store = Join-Path $PSScriptRoot '.cache/host-tools'
$manifest = Import-Csv (Join-Path $PSScriptRoot 'scripts/host-tools.manifest') -Delimiter '|'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git before using this repository.' }
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -notin @('AMD64', 'ARM64')) {
    throw 'This bootstrap supports 64-bit Windows x64 only.'
}
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Windows ARM64 is not supported by the pinned artifact list.' }
New-Item -ItemType Directory -Force $store | Out-Null

function Get-BinaryPath([string]$name, [string]$dir) {
    switch ($name) {
        'node' { return (Join-Path $dir 'node.exe') }
        'python' { return (Join-Path $dir 'python/python.exe') }
        'uv' { return (Join-Path $dir 'uv.exe') }
    }
}
function Test-Tool([string]$name, [string]$dir, [string]$digest) {
    $receipt = Join-Path $dir '.archive-sha256'
    $binaryReceipt = Join-Path $dir '.binary-sha256'
    $binary = Get-BinaryPath $name $dir
    if (-not ((Test-Path $receipt) -and (Test-Path $binaryReceipt) -and (Test-Path $binary))) { return $false }
    if ((Get-Content $receipt -Raw).Trim() -ne $digest) { return $false }
    if ((Get-FileHash $binary -Algorithm SHA256).Hash.ToLowerInvariant() -ne (Get-Content $binaryReceipt -Raw).Trim()) { return $false }
    try {
        switch ($name) {
            'node' {
                if ((& $binary --version) -ne 'v24.21.0') { return $false }
                $npm = Join-Path $dir 'npm.cmd'
                if (-not (Test-Path $npm)) { return $false }
                $oldPath = $env:PATH
                try { $env:PATH = "$dir;$oldPath"; if ((& $npm --version) -ne '11.19.0') { return $false } }
                finally { $env:PATH = $oldPath }
            }
            'python' { if ((& $binary --version) -ne 'Python 3.13.15') { return $false } }
            'uv' { if ((& $binary --version) -notmatch '^uv 0\.11\.19( |$)') { return $false } }
        }
        return $true
    } catch { return $false }
}
function Install-Tool([string]$name) {
    $override = switch ($name) {
        'node' { if ($env:B71_NODE) { $env:B71_NODE } else { $env:B71_NPM } }
        'python' { $env:B71_PYTHON }
        'uv' { $env:B71_UV }
    }
    if ($override) { Write-Host "Host tools: $name override retained."; return }
    $item = $manifest | Where-Object { $_.platform -eq 'win-x64' -and $_.tool -eq $name } | Select-Object -First 1
    if (-not $item) { throw "No pinned $name artifact for Windows x64." }
    $target = Join-Path $store "$name-$($item.version)-win-x64"
    if (Test-Tool $name $target $item.sha256) { Write-Host "Host tools: $name $($item.version) reused."; return }
    if (Test-Path $target) { throw "$name install failed verification: $target. Move it aside manually before retrying." }
    $stage = Join-Path $store ('.stage.' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $stage | Out-Null
    try {
        $archive = Join-Path $stage $(if ($item.url.EndsWith('.zip')) { 'download.zip' } else { 'download.tar.gz' })
        Invoke-WebRequest -Uri $item.url -OutFile $archive -TimeoutSec 180 -UseBasicParsing
        if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw "$name archive checksum mismatch." }
        $out = Join-Path $stage 'out'
        New-Item -ItemType Directory $out | Out-Null
        if ($item.url.EndsWith('.zip')) {
            Expand-Archive -LiteralPath $archive -DestinationPath $out
        } else {
            & tar.exe -xzf $archive -C $out
            if ($LASTEXITCODE -ne 0) { throw "$name archive extraction failed." }
        }
        switch ($name) {
            'node' { $source = Join-Path $out 'node-v24.21.0-win-x64' }
            'python' { $source = $out }
            'uv' { $source = $out }
        }
        if (-not (Test-Path $source)) { throw "$name archive layout changed." }
        $ready = Join-Path $stage 'ready'
        Move-Item -LiteralPath $source -Destination $ready
        Set-Content -LiteralPath (Join-Path $ready '.archive-sha256') -Value $item.sha256 -NoNewline
        $binary = Get-BinaryPath $name $ready
        if (-not (Test-Path $binary)) { throw "$name archive executable is missing." }
        Set-Content -LiteralPath (Join-Path $ready '.binary-sha256') -Value (Get-FileHash $binary -Algorithm SHA256).Hash.ToLowerInvariant() -NoNewline
        if (-not (Test-Tool $name $ready $item.sha256)) { throw "$name version verification failed." }
        Move-Item -LiteralPath $ready -Destination $target
        Write-Host "Host tools: $name $($item.version) installed."
    } finally {
        if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    }
}
Install-Tool node
Install-Tool python
Install-Tool uv
Write-Host 'Host tools ready. Docker Compose and a running daemon are required for setup.'
