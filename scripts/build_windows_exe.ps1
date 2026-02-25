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
$requirementsPath = Join-Path $repoRoot "requirements.txt"

if (-not (Test-Path $entryScript)) {
    throw "Entry script not found: $entryScript"
}
if (-not (Test-Path $requirementsPath)) {
    throw "Requirements file not found: $requirementsPath"
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

Write-Host "Installing/updating project dependencies..."
& $python @pythonArgs -m pip install --upgrade -r $requirementsPath

Write-Host "Verifying required imports..."
& $python @pythonArgs -c "import fastapi, uvicorn, numpy, PIL, mrcfile, matplotlib" *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Dependency verification failed. Ensure requirements install successfully, then retry."
}

Write-Host "Installing/updating PyInstaller..."
& $python @pythonArgs -m pip install --upgrade pyinstaller

$pythonExePath = (& $python @pythonArgs -c "import sys; print(sys.executable)").Trim()
$pythonHome = Split-Path -Parent $pythonExePath
$runtimeDllCandidates = @(
    "VCRUNTIME140_1.dll",
    "MSVCP140.dll"
)
$extraBinaryArgs = @()
foreach ($dllName in $runtimeDllCandidates) {
    $dllPath = Join-Path $pythonHome $dllName
    if (Test-Path $dllPath) {
        # Bundle runtime sidecars with the app to avoid LoadLibrary failures on clean hosts.
        $extraBinaryArgs += @("--add-binary", "$dllPath;.")
    }
}

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
if ($extraBinaryArgs.Count -gt 0) {
    $pyiArgs += $extraBinaryArgs
}

Write-Host "Building Windows executable..."
& $python @pythonArgs @pyiArgs

$exePath = Join-Path $repoRoot "dist\EPUMapperReview\EPUMapperReview.exe"
if (-not (Test-Path $exePath)) {
    throw "Build did not produce executable at $exePath"
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $exePath"
