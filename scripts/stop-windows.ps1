$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$RootDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RunDirectory = Join-Path $RootDirectory ".run"
$Quiet = $false

function Show-Usage {
    @"
StreamHome Windows shutdown

Usage:
  stop.bat [--quiet] [--help]

Stops only the backend and web process trees recorded by this installation.
"@ | Write-Host
}

function Stop-RecordedProcess([string]$RecordPath) {
    if (-not (Test-Path -LiteralPath $RecordPath)) { return }
    try {
        $record = Get-Content -Raw -LiteralPath $RecordPath | ConvertFrom-Json
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
        if (-not $process) { return }

        $expected = [DateTime]::Parse([string]$record.startedAt).ToUniversalTime()
        $actual = $process.StartTime.ToUniversalTime()
        if ([Math]::Abs(($actual - $expected).TotalSeconds) -gt 2) {
            if (-not $Quiet) {
                Write-Warning "Skipped reused process ID $($record.pid); its start time does not match the StreamHome record."
            }
            return
        }
        & taskkill.exe /PID ([string]$record.pid) /T /F *> $null
        if (-not $Quiet) { Write-Host "[StreamHome] Stopped $($record.kind)." }
    } catch {
        if (-not $Quiet) { Write-Warning "Could not process $RecordPath`: $($_.Exception.Message)" }
    } finally {
        Remove-Item -LiteralPath $RecordPath -Force -ErrorAction SilentlyContinue
    }
}

foreach ($argument in $args) {
    switch ($argument) {
        "--quiet" { $Quiet = $true }
        "--help" { Show-Usage; exit 0 }
        "-h" { Show-Usage; exit 0 }
        default { Write-Host "Unknown argument: $argument" -ForegroundColor Red; exit 1 }
    }
}

Stop-RecordedProcess (Join-Path $RunDirectory "web.json")
Stop-RecordedProcess (Join-Path $RunDirectory "backend.json")
if (-not $Quiet) { Write-Host "[StreamHome] Shutdown complete." -ForegroundColor Green }
