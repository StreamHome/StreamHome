$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$RepositoryUrl = "https://github.com/WaqSea/StreamHome.git"
$InstallRef = if ($env:STREAMHOME_REF) { $env:STREAMHOME_REF } else { "main" }
$InstallDirectory = if ($env:STREAMHOME_INSTALL_DIR) {
    $env:STREAMHOME_INSTALL_DIR
} else {
    Join-Path $HOME "StreamHome"
}

function Show-Usage {
    @"
StreamHome bootstrap installer

Usage:
  irm https://raw.githubusercontent.com/WaqSea/StreamHome/main/install.ps1 | iex

Environment overrides:
  STREAMHOME_INSTALL_DIR  Installation directory (default: ~/StreamHome)
  STREAMHOME_REF          Git branch or tag (default: main)

The installer clones or safely fast-forwards StreamHome and runs setup.bat.
"@ | Write-Host
}

function Write-Step([string]$Message) {
    Write-Host "`n[StreamHome] $Message" -ForegroundColor Cyan
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $wingetLinks = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"
    $env:Path = @($machinePath, $userPath, $wingetLinks, $env:Path) -join ";"
}

function Invoke-Git([string[]]$GitArguments) {
    & git.exe @GitArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($GitArguments -join ' ')"
    }
}

function Install-Git {
    if (Get-Command git.exe -ErrorAction SilentlyContinue) {
        return
    }
    Write-Step "Git is missing; attempting installation with Winget"
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Git is required. Install Git from https://git-scm.com/download/win and run the command again."
    }
    & winget.exe install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Winget could not install Git. Install it manually from https://git-scm.com/download/win."
    }
    Refresh-ProcessPath
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        $gitCandidate = Join-Path $env:ProgramFiles "Git\cmd\git.exe"
        if (Test-Path -LiteralPath $gitCandidate) {
            $env:Path = "$(Split-Path $gitCandidate);$env:Path"
        }
    }
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        throw "Git was installed but is not available in this terminal. Open a new PowerShell window and retry."
    }
}

function Test-StreamHomeRemote([string]$Remote) {
    $normalized = $Remote.Trim().TrimEnd("/")
    return $normalized -in @(
        "https://github.com/WaqSea/StreamHome",
        "https://github.com/WaqSea/StreamHome.git",
        "git@github.com:WaqSea/StreamHome.git"
    )
}

function Prepare-Checkout {
    $script:InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
    $parent = Split-Path -Parent $InstallDirectory
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    if ((Test-Path -LiteralPath $InstallDirectory) -and -not (Test-Path -LiteralPath $InstallDirectory -PathType Container)) {
        throw "The installation path exists and is not a directory: $InstallDirectory"
    }

    $gitDirectory = Join-Path $InstallDirectory ".git"
    if (Test-Path -LiteralPath $gitDirectory -PathType Container) {
        $remote = ((& git.exe -C $InstallDirectory remote get-url origin) | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not (Test-StreamHomeRemote $remote)) {
            throw "The existing directory is not a StreamHome checkout from $RepositoryUrl"
        }
        $dirty = ((& git.exe -C $InstallDirectory status --porcelain --untracked-files=normal) | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "The existing StreamHome checkout could not be inspected."
        }
        if ($dirty) {
            throw "The existing StreamHome checkout has local changes. Commit or move them before updating."
        }

        Write-Step "Updating the existing StreamHome checkout"
        Invoke-Git @("-C", $InstallDirectory, "fetch", "--depth", "1", "origin", $InstallRef)
        & git.exe -C $InstallDirectory show-ref --verify --quiet "refs/heads/$InstallRef"
        if ($LASTEXITCODE -eq 0) {
            Invoke-Git @("-C", $InstallDirectory, "checkout", $InstallRef)
            Invoke-Git @("-C", $InstallDirectory, "merge", "--ff-only", "FETCH_HEAD")
        } else {
            Invoke-Git @("-C", $InstallDirectory, "checkout", "--detach", "FETCH_HEAD")
        }
        return
    }

    if (Test-Path -LiteralPath $InstallDirectory -PathType Container) {
        $entries = @(Get-ChildItem -Force -LiteralPath $InstallDirectory)
        if ($entries.Count -gt 0) {
            throw "The installation directory is not empty and is not a StreamHome checkout: $InstallDirectory"
        }
    }

    Write-Step "Cloning StreamHome into $InstallDirectory"
    Invoke-Git @("clone", "--depth", "1", "--branch", $InstallRef, $RepositoryUrl, $InstallDirectory)
}

try {
    if ($args.Count -gt 0 -and $args[0] -in @("-Help", "--help", "-h")) {
        Show-Usage
        return
    }
    if ($args.Count -gt 0) {
        throw "Unknown argument: $($args[0])"
    }
    if ($InstallRef -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$' -or $InstallRef.Contains("..")) {
        throw "STREAMHOME_REF contains unsupported characters."
    }

    Install-Git
    Prepare-Checkout
    Write-Step "Starting StreamHome setup"
    Push-Location $InstallDirectory
    try {
        & cmd.exe /d /c setup.bat
        if ($LASTEXITCODE -ne 0) {
            throw "setup.bat exited with code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
} catch {
    Write-Host "`n[StreamHome] ERROR: $($_.Exception.Message)" -ForegroundColor Red
    throw
}
