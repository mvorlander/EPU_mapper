param(
    [string]$Version = "0.1.0",
    [switch]$SkipExeBuild = $false
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "This script must be run on Windows."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$exeBuilder = Join-Path $scriptDir "build_windows_exe.ps1"
$issPath = Join-Path $repoRoot "windows\EPUMapperReview.iss"
$distFolder = Join-Path $repoRoot "dist\EPUMapperReview"

if (-not $SkipExeBuild) {
    & $exeBuilder
}

if (-not (Test-Path $distFolder)) {
    throw "Missing $distFolder. Run scripts/build_windows_exe.ps1 first."
}
if (-not (Test-Path $issPath)) {
    throw "Missing Inno Setup script: $issPath"
}

$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
    $defaultIscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    if (Test-Path $defaultIscc) {
        $isccPath = $defaultIscc
    } else {
        throw "Inno Setup Compiler (ISCC.exe) not found. Install Inno Setup 6 and retry."
    }
} else {
    $isccPath = $iscc.Source
}

Write-Host "Building installer..."
& $isccPath "/DMyAppVersion=$Version" $issPath

$installerOut = Join-Path $repoRoot "dist\installer"
Write-Host ""
Write-Host "Installer build complete. Output directory:"
Write-Host "  $installerOut"
