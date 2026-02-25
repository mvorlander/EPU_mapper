param(
    [switch]$Clean = $true
)

$ErrorActionPreference = "Stop"

$runningOnWindows = $IsWindows
if ($null -eq $runningOnWindows) {
    $runningOnWindows = ($env:OS -eq "Windows_NT")
}

if (-not $runningOnWindows) {
    throw "This script must be run on Windows."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$entryScript = Join-Path $repoRoot "scripts\windows_gui_launcher.py"
$srcPath = Join-Path $repoRoot "src"

if (-not (Test-Path $entryScript)) {
    throw "Entry script not found: $entryScript"
}

function Test-PythonCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [string[]]$Args = @()
    )

    try {
        & $Command @Args -c "import sys; print(sys.version)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$python = $null
$pythonArgs = @()

if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-PythonCommand -Command "py" -Args @("-3"))) {
    $python = "py"
    $pythonArgs = @("-3")
} elseif ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-PythonCommand -Command "python")) {
    $python = "python"
    $pythonArgs = @()
} else {
    $pythonCandidates = Get-ChildItem -Path (Join-Path $env:LocalAppData "Programs\Python") -Directory -Filter "Python*" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "python.exe" }
    $pythonFromInstall = $pythonCandidates | Where-Object { (Test-Path $_) -and (Test-PythonCommand -Command $_) } | Select-Object -First 1
    if (-not $pythonFromInstall) {
        throw "Python was not found. Install Python 3.11+ and retry."
    }
    $python = $pythonFromInstall
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
