$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) { $npm = "npm.cmd" }

$server = Join-Path $root "server"
$web = Join-Path $root "web"
$env:PYTHONPATH = "."

$serverChecks = @(
    "scratch/test_cloud_streaming.py",
    "scratch/test_drive_setup.py",
    "scratch/test_ffmpeg_headers.py",
    "scratch/test_ingest_stream_script.py",
    "scratch/test_playback_contract.py",
    "scratch/test_playback_pipeline.py",
    "scratch/test_queue_failure_handling.py",
    "scratch/test_rclone_fallback.py",
    "scratch/test_recommendation_system.py",
    "scratch/test_search_caching.py",
    "scratch/test_setup_scripts.py",
    "scratch/test_vibe_analysis.py"
)

Push-Location $server
try {
    foreach ($check in $serverChecks) {
        Write-Host "[server] $check"
        & $python $check
        if ($LASTEXITCODE -ne 0) { throw "$check failed with exit code $LASTEXITCODE" }
    }
    Write-Host "[server] scratch/check_db.py"
    & $python "scratch/check_db.py"
    if ($LASTEXITCODE -ne 0) { throw "Database checker failed with exit code $LASTEXITCODE" }
}
finally { Pop-Location }

Push-Location $web
try {
    foreach ($command in @("test", "lint", "build")) {
        Write-Host "[web] npm run $command"
        & $npm run $command
        if ($LASTEXITCODE -ne 0) { throw "npm run $command failed with exit code $LASTEXITCODE" }
    }
}
finally { Pop-Location }

Write-Host "All non-security release checks passed."
