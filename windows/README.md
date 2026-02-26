# Windows Packaging Notes

This folder contains `EPUMapperReview.iss`, the Inno Setup script that powers
the Windows installer distributed on GitHub.

## Release builds (preferred)

Publishing a git tag triggers the `windows-build` GitHub Actions workflow, which
produces both the installer (`EPUMapperReviewInstaller_<version>.exe`) and the
portable ZIP (`EPUMapperReview_portable_<version>.zip`). The artifacts are
attached to the corresponding Release entry, so end users only have to download
and run the installer.

## Manual build workflow

If you need to test installer changes locally before pushing a tag:

```powershell
cd C:\path\to\EPU_mapper
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_installer.ps1 -Version <version>
```

The script:

- builds `dist\EPUMapperReview\EPUMapperReview.exe` with PyInstaller
- builds `dist\installer\EPUMapperReviewInstaller_<version>.exe` with Inno Setup

To generate only the portable `.exe` folder (no installer), run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows_exe.ps1 -Version <version>
```

Keep `EPUMapperReview.exe` and its `_internal\` folder together when sharing
portable builds manually. The huge `_internal\` directory contains the embedded
Python runtime; copying only the `.exe` will result in a broken distribution.

Requirements for a maintainer machine:

- Windows with Python 3.11+
- Inno Setup 6 on the `PATH`

Update `windows/EPUMapperReview.iss` before cutting a new version if you need
to change icons, shortcuts, or metadata baked into the installer UI.
