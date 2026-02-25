param(
    [switch]$Clean = $true
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "This script must be run on Windows."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$entryScript = Join-Path $repoRoot "scripts\windows_gui_launcher.py"
$srcPath = Join-Path $repoRoot "src"

if (-not (Test-Path $entryScript)) {
    throw "Entry script not found: $entryScript"
}

$pythonCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCmd) {
    $pythonArgs = @("-3")
    $python = "py"
} else {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        throw "Python was not found on PATH. Install Python 3.11+ and retry."
    }
    $pythonArgs = @()
    $python = "python"
}

if ($Clean) {
    $buildDir = Join-Path $repoRoot "build"
    $distDir = Join-Path $repoRoot "dist"
    if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
    if (Test-Path $distDir) { Remove-Item -Recurse -Force $distDir }
}

Write-Host "Installing/updating PyInstaller..."
& $python @pythonArgs -m pip install --upgrade pyinstaller

$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--name", "EPUMapperReview",
    "--paths", $srcPath,
    "--paths", $repoRoot,
    "--hidden-import", "review_app",
    "--hidden-import", "build_collage",
    "--hidden-import", "scripts.plot_foilhole_positions",
    "--hidden-import", "matplotlib.backends.backend_tkagg",
    "--collect-submodules", "matplotlib",
    $entryScript
)

Write-Host "Building Windows executable..."
& $python @pythonArgs @pyiArgs

$exePath = Join-Path $repoRoot "dist\EPUMapperReview\EPUMapperReview.exe"
if (-not (Test-Path $exePath)) {
    throw "Build did not produce executable at $exePath"
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $exePath"
