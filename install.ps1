# FDA System Installation Script for Windows
# Usage: irm https://raw.githubusercontent.com/white-dots/FDA/main/install.ps1 | iex
# Or: .\install.ps1

$ErrorActionPreference = "Stop"

Write-Host "==================================" -ForegroundColor Cyan
Write-Host "FDA System Installer for Windows" -ForegroundColor Cyan
Write-Host "==================================" -ForegroundColor Cyan
Write-Host ""

# Check if running as administrator (optional, for PATH modification)
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# Find Python
function Find-Python {
    $pythonCandidates = @(
        "python3.12",
        "python3.11",
        "python3.10",
        "python3.9",
        "python3",
        "python"
    )

    foreach ($py in $pythonCandidates) {
        try {
            $version = & $py --version 2>&1
            if ($version -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 9) {
                    return $py
                }
            }
        } catch {
            continue
        }
    }

    return $null
}

$pythonCmd = Find-Python

if (-not $pythonCmd) {
    Write-Host "Error: Python 3.9+ not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Python from: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Make sure to check 'Add Python to PATH' during installation!" -ForegroundColor Yellow
    exit 1
}

$pythonVersion = & $pythonCmd --version
Write-Host "Using Python: $pythonCmd ($pythonVersion)" -ForegroundColor Green
Write-Host ""

# Set install directory
$installDir = if ($env:FDA_INSTALL_DIR) { $env:FDA_INSTALL_DIR } else { "$env:USERPROFILE\FDA" }

# Check if git is available
try {
    $null = git --version
} catch {
    Write-Host "Error: Git not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Git from: https://git-scm.com/download/win" -ForegroundColor Yellow
    exit 1
}

# Clone or update repository
if (Test-Path $installDir) {
    Write-Host "Updating existing installation at $installDir..."
    Push-Location $installDir
    git pull
} else {
    Write-Host "Cloning FDA repository to $installDir..."
    git clone https://github.com/white-dots/FDA.git $installDir
    Push-Location $installDir
}

Write-Host ""

# Create virtual environment
if (-not (Test-Path "venv")) {
    Write-Host "Creating virtual environment..."
    & $pythonCmd -m venv venv
}

# Activate virtual environment
Write-Host "Activating virtual environment..."
$activateScript = ".\venv\Scripts\Activate.ps1"
if (Test-Path $activateScript) {
    . $activateScript
} else {
    # Try batch file activation
    $activateBat = ".\venv\Scripts\activate.bat"
    if (Test-Path $activateBat) {
        cmd /c $activateBat
    }
}

# Upgrade pip
Write-Host "Upgrading pip..."
& .\venv\Scripts\python.exe -m pip install --upgrade pip

# Install FDA with all dependencies
Write-Host "Installing FDA and dependencies..."
& .\venv\Scripts\pip.exe install -e ".[all]"

Pop-Location

Write-Host ""
Write-Host "==================================" -ForegroundColor Green
Write-Host "Installation Complete!" -ForegroundColor Green
Write-Host "==================================" -ForegroundColor Green
Write-Host ""
Write-Host "To get started:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Open PowerShell and navigate to FDA:" -ForegroundColor White
Write-Host "     cd $installDir" -ForegroundColor Yellow
Write-Host ""
Write-Host "  2. Activate the environment:" -ForegroundColor White
Write-Host "     .\venv\Scripts\Activate.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "  3. Start the setup server:" -ForegroundColor White
Write-Host "     fda setup" -ForegroundColor Yellow
Write-Host ""
Write-Host "  4. Open http://localhost:9999 in your browser" -ForegroundColor White
Write-Host ""
Write-Host "For more commands, run: fda --help" -ForegroundColor Cyan
Write-Host ""

# Create a convenience batch file
$batchContent = @"
@echo off
cd /d "$installDir"
call venv\Scripts\activate.bat
fda %*
"@

$batchPath = "$installDir\fda.bat"
$batchContent | Out-File -FilePath $batchPath -Encoding ASCII

Write-Host "Created convenience script: $batchPath" -ForegroundColor Gray
Write-Host ""
Write-Host "Optional: Add $installDir to your PATH to run 'fda' from anywhere." -ForegroundColor Gray
Write-Host ""
