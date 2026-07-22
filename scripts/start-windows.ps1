$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$RootDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RunDirectory = Join-Path $RootDirectory ".run"
$EnvironmentPath = Join-Path $RootDirectory ".env"

function Show-Usage {
    @"
StreamHome Windows startup

Usage:
  start.bat [--help]

Starts the production API and web application in hidden background processes.
Use stop.bat to stop only the processes created by this installation.
"@ | Write-Host
}

function Read-EnvironmentFile {
    $values = @{}
    if (-not (Test-Path -LiteralPath $EnvironmentPath)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $EnvironmentPath) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $value = $Matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$Matches[1]] = $value
        }
    }
    return $values
}

function New-BootstrapCode {
    $bytes = New-Object byte[] 24
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Normalize-ProcessEnvironment {
    # Some launchers provide both Path and PATH. Start-Process treats those as
    # duplicate case-insensitive dictionary keys on Windows and refuses to run.
    $currentPath = $env:Path
    [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
    [Environment]::SetEnvironmentVariable("Path", $null, "Process")
    [Environment]::SetEnvironmentVariable("Path", $currentPath, "Process")
}

function Save-ProcessRecord([string]$Path, [Diagnostics.Process]$Process, [string]$Kind) {
    [pscustomobject]@{
        pid = $Process.Id
        kind = $Kind
        startedAt = $Process.StartTime.ToUniversalTime().ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Wait-ForEndpoint([string]$Url, [int]$Attempts = 40) {
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return $true
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Assert-PortAvailable([int]$Port, [string]$Label) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $Port)
    try {
        $listener.Start()
    } catch {
        throw "$Label port $Port is already in use. Stop the conflicting service and run start.bat again."
    } finally {
        try { $listener.Stop() } catch { }
    }
}

try {
    if ($args.Count -gt 0) {
        if ($args[0] -in @("--help", "-h")) { Show-Usage; exit 0 }
        throw "Unknown argument: $($args[0])"
    }

    $settings = Read-EnvironmentFile
    $webPortText = if ($settings.ContainsKey("WEB_PORT")) { $settings["WEB_PORT"] } else { "3000" }
    $webPort = 0
    if (-not [int]::TryParse($webPortText, [ref]$webPort) -or $webPort -lt 1 -or $webPort -gt 65535) {
        throw "Invalid WEB_PORT in .env: $webPortText"
    }
    if ($webPort -eq 8000) { throw "WEB_PORT cannot be 8000 because the API uses that port." }
    $setupValue = if ($settings.ContainsKey("SETUP")) { $settings["SETUP"] } else { "false" }
    $setupActive = $setupValue -notin @("true", "1")
    $publicUrl = if ($settings.ContainsKey("PUBLIC_URL")) { $settings["PUBLIC_URL"] } else { "http://localhost:$webPort" }
    $bootstrapCode = if ($setupActive) { New-BootstrapCode } else { "" }

    $python = Join-Path $RootDirectory "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw "Python is unavailable. Run setup.bat first." }
        $python = $pythonCommand.Source
    }
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) { throw "npm is unavailable. Run setup.bat first." }

    if (-not (Test-Path -LiteralPath $RunDirectory)) {
        New-Item -ItemType Directory -Path $RunDirectory -Force | Out-Null
    }
    & (Join-Path $PSScriptRoot "stop-windows.ps1") --quiet
    Assert-PortAvailable 8000 "API"
    Assert-PortAvailable $webPort "Web"

    $env:WEB_PORT = [string]$webPort
    $env:SETUP = $setupValue
    $env:PUBLIC_URL = $publicUrl
    $env:STREAMHOME_SETUP_CODE = $bootstrapCode
    $env:NODE_ENV = "production"
    Normalize-ProcessEnvironment

    Write-Host "[StreamHome] Starting API on 127.0.0.1:8000..."
    $backend = Start-Process -FilePath $python -ArgumentList @("main.py") `
        -WorkingDirectory (Join-Path $RootDirectory "server") `
        -RedirectStandardOutput (Join-Path $RootDirectory "backend.log") `
        -RedirectStandardError (Join-Path $RootDirectory "backend-error.log") `
        -WindowStyle Hidden -PassThru
    Save-ProcessRecord (Join-Path $RunDirectory "backend.json") $backend "backend"

    Write-Host "[StreamHome] Starting web on 0.0.0.0:$webPort..."
    $web = Start-Process -FilePath $npmCommand.Source -ArgumentList @("run", "server") `
        -WorkingDirectory (Join-Path $RootDirectory "web") `
        -RedirectStandardOutput (Join-Path $RootDirectory "frontend.log") `
        -RedirectStandardError (Join-Path $RootDirectory "frontend-error.log") `
        -WindowStyle Hidden -PassThru
    Save-ProcessRecord (Join-Path $RunDirectory "web.json") $web "web"

    Start-Sleep -Milliseconds 750
    $backend.Refresh()
    $web.Refresh()
    if ($backend.HasExited -or $web.HasExited) {
        & (Join-Path $PSScriptRoot "stop-windows.ps1") --quiet
        throw "A StreamHome process exited during startup. Review backend-error.log and frontend-error.log."
    }

    $apiReady = Wait-ForEndpoint "http://127.0.0.1:8000/api/health"
    $webReady = Wait-ForEndpoint "http://127.0.0.1:$webPort/"
    if (-not $apiReady -or -not $webReady) {
        & (Join-Path $PSScriptRoot "stop-windows.ps1") --quiet
        throw "Startup health checks failed. Review backend-error.log and frontend-error.log."
    }

    Write-Host "`n[StreamHome] Running at http://localhost:$webPort" -ForegroundColor Green
    if ($setupActive) {
        Write-Host "[StreamHome] Setup URL: http://localhost:$webPort/setup"
        Write-Host "[StreamHome] One-time bootstrap code: $bootstrapCode" -ForegroundColor Yellow
    }
    Write-Host "[StreamHome] Logs: backend.log, backend-error.log, frontend.log, frontend-error.log"
} catch {
    Write-Host "`n[StreamHome] ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
