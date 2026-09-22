# ⛔【已废弃·DESKTOP/ELECTRON 退役 2026-08·全量WEB】桌面打包用,web 不用。见 docs/DESKTOP-RETIRED.md
# Phase 2.11l — fetch LGPL FFmpeg binaries into vendor/ffmpeg/<plat>/.
#
# We don't commit the ~190MB blob to git. Run this once per checkout
# before `npm run electron:build`, or any time the bundle is missing.
# CI / fresh dev machines should run this immediately after `git clone`.
#
# Mirror: BtbN/FFmpeg-Builds GitHub releases. We grab the
# "lgpl-shared" build — no x264/x265, so commercially safe to ship.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$repo = Split-Path -Parent $PSScriptRoot
$dst  = Join-Path $repo 'vendor\ffmpeg\win64'
$tmp  = Join-Path $env:TEMP 'ffmpeg-btbn.zip'
$ext  = Join-Path $env:TEMP 'ffmpeg-extract'

# Skip if already present and working.
if (Test-Path "$dst\ffmpeg.exe") {
    $version = & "$dst\ffmpeg.exe" -version 2>&1 | Select-Object -First 1
    if ($LASTEXITCODE -eq 0) {
        Write-Output "FFmpeg already in place: $version"
        Write-Output "Delete vendor/ffmpeg/win64/ to force re-download."
        exit 0
    }
}

New-Item -ItemType Directory -Path $dst -Force | Out-Null

$url = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl-shared.zip'
Write-Output "Downloading from BtbN GitHub mirror (~83MB compressed)..."
$start = Get-Date
Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
$elapsed = ((Get-Date) - $start).TotalSeconds
$mb = (Get-Item $tmp).Length / 1MB
Write-Output ("Downloaded {0:N1} MB in {1:N1}s" -f $mb, $elapsed)

Remove-Item $ext -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive -Path $tmp -DestinationPath $ext
$src = Join-Path $ext 'ffmpeg-master-latest-win64-lgpl-shared\bin'

# Copy what we ship in the installer. ffplay.exe is excluded — we don't
# need playback. avdevice is required (DLL chain).
$keep = @(
    'ffmpeg.exe', 'ffprobe.exe',
    'avcodec-62.dll', 'avdevice-62.dll', 'avfilter-11.dll',
    'avformat-62.dll', 'avutil-60.dll',
    'swresample-6.dll', 'swscale-9.dll'
)
foreach ($f in $keep) {
    Copy-Item (Join-Path $src $f) $dst -Force
}

# Clean up scratch.
Remove-Item $tmp -Force -ErrorAction SilentlyContinue
Remove-Item $ext -Recurse -Force -ErrorAction SilentlyContinue

# Verify.
$out = & "$dst\ffmpeg.exe" -version 2>&1 | Select-Object -First 1
if ($LASTEXITCODE -ne 0) {
    Write-Error "FFmpeg sanity check failed: $out"
    exit 1
}
$total = (Get-ChildItem $dst | Measure-Object Length -Sum).Sum / 1MB
Write-Output ("Done. {0:N1} MB at {1}" -f $total, $dst)
Write-Output $out
