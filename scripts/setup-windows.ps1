$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$RootDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$NoStart = $false
$SkipSystemPackages = $false
$script:PythonExecutable = $null
$script:PythonPrefix = @()

function Show-Usage {
    @"
StreamHome Windows setup

Usage:
  setup.bat [--no-start] [--skip-system-packages] [--help]

Options:
  --no-start             Install and build without starting StreamHome.
  --skip-system-packages Do not install missing operating-system packages.
  --help                 Show this help text.
"@ | Write-Host
}

function Write-Step([string]$Message) {
    Write-Host "`n[StreamHome Setup] $Message" -ForegroundColor Cyan
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $wingetLinks = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"
    $localPrograms = Join-Path $env:LOCALAPPDATA "Programs"
    $env:Path = @($machinePath, $userPath, $wingetLinks, $localPrograms, $env:Path) -join ";"
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Description) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Test-PythonVersion([string]$Executable, [string[]]$Prefix) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Executable @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Resolve-Python {
    $script:PythonExecutable = $null
    $script:PythonPrefix = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        if (Test-PythonVersion $launcher.Source @("-3.11")) {
            $script:PythonExecutable = $launcher.Source
            $script:PythonPrefix = @("-3.11")
            return $true
        }
        if (Test-PythonVersion $launcher.Source @("-3")) {
            $script:PythonExecutable = $launcher.Source
            $script:PythonPrefix = @("-3")
            return $true
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python -and (Test-PythonVersion $python.Source @())) {
        $script:PythonExecutable = $python.Source
        return $true
    }
    return $false
}

function Invoke-Python([string[]]$Arguments, [string]$Description) {
    $combined = @($script:PythonPrefix) + $Arguments
    Invoke-Checked $script:PythonExecutable $combined $Description
}

function Install-WingetPackage([string]$Id, [string]$Label) {
    Write-Step "Installing $Label"
    Invoke-Checked "winget.exe" @(
        "install", "--id", $Id, "--exact", "--source", "winget",
        "--accept-package-agreements", "--accept-source-agreements",
        "--disable-interactivity"
    ) "$Label installation"
}

function Ensure-SystemDependencies {
    Refresh-ProcessPath
    $missing = New-Object System.Collections.Generic.List[string]
    if (-not (Resolve-Python)) { $missing.Add("python") }
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) { $missing.Add("node") }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { $missing.Add("npm") }
    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) { $missing.Add("ffmpeg") }
    if (-not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)) { $missing.Add("ffprobe") }
    if (-not (Get-Command rclone.exe -ErrorAction SilentlyContinue)) { $missing.Add("rclone") }

    if ($missing.Count -gt 0) {
        if ($SkipSystemPackages) {
            throw "Missing required commands: $($missing -join ', '). Install them manually or omit --skip-system-packages."
        }
        if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            throw "Missing required commands: $($missing -join ', '). Install Python 3.11+, Node.js 18+, FFmpeg, and rclone, or install Winget."
        }
        if ($missing -contains "python") { Install-WingetPackage "Python.Python.3.11" "Python 3.11" }
        if ($missing -contains "node" -or $missing -contains "npm") { Install-WingetPackage "OpenJS.NodeJS.LTS" "Node.js LTS" }
        if ($missing -contains "ffmpeg" -or $missing -contains "ffprobe") { Install-WingetPackage "Gyan.FFmpeg" "FFmpeg" }
        if ($missing -contains "rclone") { Install-WingetPackage "Rclone.Rclone" "rclone" }
        Refresh-ProcessPath
    }

    if (-not (Resolve-Python)) { throw "Python 3.11 or newer is required and was not found after installation." }
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $node -or -not $npm) { throw "Node.js and npm are required and were not found after installation." }
    $nodeMajorText = ((& $node.Source -p "process.versions.node.split('.')[0]") | Out-String).Trim()
    $nodeMajor = 0
    if (-not [int]::TryParse($nodeMajorText, [ref]$nodeMajor) -or $nodeMajor -lt 18) {
        throw "Node.js 18 or newer is required."
    }
    foreach ($command in @("ffmpeg.exe", "ffprobe.exe", "rclone.exe")) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "$command is required and was not found after installation. Open a new terminal and run setup.bat again."
        }
    }
}

function Prepare-VirtualEnvironment {
    $venvDirectory = Join-Path $RootDirectory "venv"
    $venvPython = Join-Path $venvDirectory "Scripts\python.exe"
    if ((Test-Path -LiteralPath $venvDirectory) -and -not (Test-Path -LiteralPath $venvPython)) {
        $recovery = "$venvDirectory.broken.$(Get-Date -Format 'yyyyMMddHHmmss')"
        Write-Step "Moving the incomplete virtual environment to $recovery"
        Move-Item -LiteralPath $venvDirectory -Destination $recovery
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Step "Creating the Python virtual environment"
        Invoke-Python @("-m", "venv", $venvDirectory) "Python virtual environment creation"
    }

    Write-Step "Installing server dependencies"
    Invoke-Checked $venvPython @("-m", "pip", "install", "--upgrade", "pip") "pip upgrade"
    Invoke-Checked $venvPython @("-m", "pip", "install", "-r", (Join-Path $RootDirectory "server\requirements.txt")) "server dependency installation"
}

function Prepare-Web {
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    Push-Location (Join-Path $RootDirectory "web")
    try {
        Write-Step "Installing locked web dependencies"
        Invoke-Checked $npm @("ci") "web dependency installation"
        Write-Step "Building the production web application"
        Invoke-Checked $npm @("run", "build") "production web build"
    } finally {
        Pop-Location
    }
}

function Prepare-Environment {
    $environmentPath = Join-Path $RootDirectory ".env"
    if (-not (Test-Path -LiteralPath $environmentPath)) {
        $examplePath = Join-Path $RootDirectory ".env.example"
        if (Test-Path -LiteralPath $examplePath) {
            Copy-Item -LiteralPath $examplePath -Destination $environmentPath
        } else {
            "SETUP=false`r`nWEB_PORT=3000`r`n" | Set-Content -LiteralPath $environmentPath -Encoding ASCII
        }
        Write-Step "Created .env with first-run setup enabled"
    } else {
        Write-Step "Preserving the existing .env configuration"
    }
}

try {
    foreach ($argument in $args) {
        switch ($argument) {
            "--no-start" { $NoStart = $true }
            "--skip-system-packages" { $SkipSystemPackages = $true }
            "--help" { Show-Usage; return }
            "-h" { Show-Usage; return }
            default { throw "Unknown argument: $argument (use --help for usage)" }
        }
    }

    Write-Step "Preparing StreamHome in $RootDirectory"
    Ensure-SystemDependencies
    Prepare-VirtualEnvironment
    Prepare-Web
    Prepare-Environment
    Write-Step "Setup dependencies and production assets are ready"

    if ($NoStart) {
        Write-Host "[StreamHome Setup] Start later with: start.bat"
        exit 0
    }
    & cmd.exe /d /c (Join-Path $RootDirectory "start.bat")
    if ($LASTEXITCODE -ne 0) {
        throw "StreamHome startup failed with exit code $LASTEXITCODE."
    }
} catch {
    Write-Host "`n[StreamHome Setup] ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "[StreamHome Setup] Fix the reported problem and run setup.bat again; existing data was not removed." -ForegroundColor Yellow
    exit 1
}
