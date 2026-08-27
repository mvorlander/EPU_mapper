#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_PATH="$ROOT_DIR/macos/EPUMapperLauncher.applescript"
LAUNCHER_PATH="$ROOT_DIR/scripts/macos_gui_launcher.py"
OUTPUT_DIR="$ROOT_DIR/dist/macos"
APP_PATH="$OUTPUT_DIR/EPU Mapper.app"
INSTALL_APP=false
PYTHON_BIN="${EPU_MAPPER_PYTHON:-}"

usage() {
  echo "Usage: $0 [--python /path/to/python] [--install]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      PYTHON_BIN="$2"
      shift 2
      ;;
    --install)
      INSTALL_APP=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "The macOS launcher can only be built on macOS." >&2
  exit 1
fi

python_is_usable() {
  local candidate="$1"
  [[ -x "$candidate" ]] || return 1
  "$candidate" -c 'import fastapi, mrcfile, PIL, reportlab, tkinter, uvicorn' >/dev/null 2>&1
}

if [[ -n "$PYTHON_BIN" ]] && ! python_is_usable "$PYTHON_BIN"; then
  echo "Python at '$PYTHON_BIN' is missing one or more EPU Mapper dependencies." >&2
  exit 1
fi

if [[ -z "$PYTHON_BIN" ]]; then
  candidates=(
    "/opt/anaconda3/envs/epu-mapper/bin/python"
    "/opt/anaconda3/envs/EPU_mapping/bin/python"
    "$HOME/miniconda3/envs/epu-mapper/bin/python"
    "$HOME/miniconda3/envs/EPU_mapping/bin/python"
  )
  if command -v python >/dev/null 2>&1; then
    candidates+=("$(command -v python)")
  fi
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi
  for candidate in "${candidates[@]}"; do
    if python_is_usable "$candidate"; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "No usable EPU Mapper Python environment was found." >&2
  echo "Create it with 'conda env create -f environment.yml', then pass --python /path/to/env/bin/python." >&2
  exit 1
fi

OVERLAY_HELPER_PATH="$ROOT_DIR/scripts/plot_foilhole_positions.py"
if [[ ! -f "$TEMPLATE_PATH" || ! -f "$LAUNCHER_PATH" || ! -f "$OVERLAY_HELPER_PATH" || ! -d "$ROOT_DIR/src" ]]; then
  echo "Launcher sources are missing from $ROOT_DIR." >&2
  exit 1
fi

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

mkdir -p "$OUTPUT_DIR"
source_file="$(mktemp "${TMPDIR:-/tmp}/epu-mapper-launcher.XXXXXX")"
trap 'rm -f "$source_file"' EXIT
python_escaped="$(escape_sed_replacement "$PYTHON_BIN")"
sed -e "s|__PYTHON_PATH__|$python_escaped|g" "$TEMPLATE_PATH" > "$source_file"

if [[ -e "$APP_PATH" ]]; then
  rm -rf "$APP_PATH"
fi
/usr/bin/osacompile -o "$APP_PATH" "$source_file"
RUNTIME_DIR="$APP_PATH/Contents/Resources/runtime"
mkdir -p "$RUNTIME_DIR/src" "$RUNTIME_DIR/scripts"
/usr/bin/ditto "$ROOT_DIR/src/build_collage.py" "$RUNTIME_DIR/src/build_collage.py"
/usr/bin/ditto "$ROOT_DIR/src/review_app.py" "$RUNTIME_DIR/src/review_app.py"
/usr/bin/ditto "$ROOT_DIR/scripts/windows_gui_launcher.py" "$RUNTIME_DIR/scripts/windows_gui_launcher.py"
/usr/bin/ditto "$ROOT_DIR/scripts/macos_gui_launcher.py" "$RUNTIME_DIR/scripts/macos_gui_launcher.py"
/usr/bin/ditto "$OVERLAY_HELPER_PATH" "$RUNTIME_DIR/scripts/plot_foilhole_positions.py"
PYTHONPATH="$RUNTIME_DIR/src:$RUNTIME_DIR" "$PYTHON_BIN" -c \
  'from scripts.plot_foilhole_positions import compute_markers, plot_overlay, set_forced_transform; assert callable(compute_markers) and callable(plot_overlay) and callable(set_forced_transform)'
PLIST_PATH="$APP_PATH/Contents/Info.plist"
privacy_keys=(
  NSAppleMusicUsageDescription
  NSCalendarsUsageDescription
  NSCameraUsageDescription
  NSContactsUsageDescription
  NSHomeKitUsageDescription
  NSMicrophoneUsageDescription
  NSPhotoLibraryUsageDescription
  NSRemindersUsageDescription
  NSSiriUsageDescription
  NSSystemAdministrationUsageDescription
)
for privacy_key in "${privacy_keys[@]}"; do
  /usr/libexec/PlistBuddy -c "Delete :$privacy_key" "$PLIST_PATH" >/dev/null 2>&1 || true
done
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string org.plaschka.epumapper.launcher" "$PLIST_PATH"
release_version="$(git -C "$ROOT_DIR" describe --tags --abbrev=0 2>/dev/null || echo v0.0.0)"
release_version="${release_version#v}"
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $release_version" "$PLIST_PATH"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $release_version" "$PLIST_PATH"
/usr/bin/xattr -cr "$APP_PATH"
/usr/bin/codesign --force --deep --sign - "$APP_PATH" >/dev/null 2>&1

echo "Built: $APP_PATH"
echo "Python: $PYTHON_BIN"

if $INSTALL_APP; then
  INSTALL_DIR="$HOME/Applications"
  INSTALL_PATH="$INSTALL_DIR/EPU Mapper.app"
  mkdir -p "$INSTALL_DIR"
  if [[ -e "$INSTALL_PATH" ]]; then
    rm -rf "$INSTALL_PATH"
  fi
  /usr/bin/ditto "$APP_PATH" "$INSTALL_PATH"
  echo "Installed: $INSTALL_PATH"
fi
