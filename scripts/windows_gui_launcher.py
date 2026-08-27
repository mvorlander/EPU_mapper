#!/usr/bin/env python3
"""Lightweight Tkinter launcher for the EPU Mapper review app."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception:  # pragma: no cover - only hit on systems without Tk support
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
from portable_session import (  # noqa: E402
    PORTABLE_SESSION_FILENAME,
    export_portable_session,
    load_portable_session,
    portable_bundle_name,
    portable_session_source,
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8000"
DEFAULT_LABEL = os.environ.get("SESSION_LABEL") or os.environ.get("GRID_LABEL") or os.environ.get("REPORT_PREFIX") or ""
ATLAS_MODE_EPU = "epu"
ATLAS_MODE_STATIC = "static"
ATLAS_MODE_OPTIONS = [
    ("Use EPU atlas data (Recommended)", ATLAS_MODE_EPU),
    ("Use atlas screenshot with screened GridSquares", ATLAS_MODE_STATIC),
]
TRANSFORM_OPTIONS = [
    ("Identity (default)", "identity"),
    ("Auto detect", "auto"),
    ("Rotate 90°", "rot90"),
    ("Rotate 180°", "rot180"),
    ("Rotate 270°", "rot270"),
    ("Mirror X", "mirror_x"),
    ("Mirror Y", "mirror_y"),
    ("Mirror diag", "mirror_diag"),
    ("Mirror diag inv", "mirror_diag_inv"),
]


def _browser_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _startup_page_html(target_url: str) -> str:
    target_json = json.dumps(target_url).replace("</", "<\\/")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EPU Mapper is preparing</title>
<style>:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f7fb;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{width:min(560px,calc(100vw - 36px));background:#fff;border:1px solid #dfe5ee;border-radius:16px;padding:28px;box-shadow:0 18px 50px rgba(26,39,67,.12)}}main.error{{border:2px solid #e11d48;background:#fff7f8}}.mark{{width:42px;height:42px;border-radius:12px;background:linear-gradient(145deg,#5eead4,#3b82f6);display:grid;place-items:center;font-weight:850;color:#10213b;margin-bottom:18px}}main.error .mark{{background:#e11d48;color:#fff}}h1{{font-size:21px;margin:0 0 8px}}p{{font-size:13px;line-height:1.55;color:#657289;margin:0}}.progress{{height:5px;background:#e8edf5;border-radius:9px;overflow:hidden;margin:22px 0 14px}}.progress span{{display:block;width:35%;height:100%;background:#2563eb;border-radius:9px;animation:move 1.4s ease-in-out infinite}}#status{{font-size:12px;color:#7a8799;white-space:pre-wrap;line-height:1.5}}main.error #status{{margin-top:18px;padding:14px;border-radius:10px;background:#ffe4e6;color:#9f1239;font-size:14px;font-weight:700}}main.error .progress{{display:none}}@keyframes move{{0%{{transform:translateX(-110%)}}100%{{transform:translateX(330%)}}}}</style></head>
<body><main id="launch-card"><div class="mark" id="launch-mark">E</div><h1 id="launch-title">Preparing the screening dashboard</h1><p id="launch-description">EPU Mapper is reading the session and atlas. Sessions on OffloadData can take a few minutes; this page will open the dashboard automatically.</p><div class="progress"><span></span></div><div id="status">Waiting for the local server…</div></main>
<script>const target={target_json};async function poll(){{try{{const response=await fetch('/ready?t='+Date.now(),{{cache:'no-store'}});const state=await response.json();if(state.ready){{location.replace(state.url);return}}if(state.error){{document.getElementById('launch-card').classList.add('error');document.getElementById('launch-mark').textContent='!';document.getElementById('launch-title').textContent='Server launch failed';document.getElementById('launch-description').textContent='Return to the EPU Mapper launcher for the full error and troubleshooting log.';document.getElementById('status').textContent=state.error;return}}}}catch(_error){{}}setTimeout(poll,1000)}}poll();</script></body></html>"""


def _start_browser_wait_page(
    proc: subprocess.Popen[str], host: str, port: str, error_state: dict[str, str | None] | None = None
) -> tuple[ThreadingHTTPServer, str]:
    connect_host = _browser_host(host)
    target_url = f"http://{connect_host}:{port}"
    page = _startup_page_html(target_url).encode("utf-8")

    class StartupHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path.startswith("/ready"):
                error = None
                ready = False
                exit_code = proc.poll()
                if exit_code is not None:
                    error = (error_state or {}).get("message") or (
                        f"EPU Mapper stopped before the server was ready (exit code {exit_code}). Check the launcher log."
                    )
                else:
                    try:
                        with socket.create_connection((connect_host, int(port)), timeout=0.25):
                            ready = True
                    except OSError:
                        pass
                payload = json.dumps({"ready": ready, "url": target_url, "error": error}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), StartupHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wait_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, wait_url


def _server_failure_message(exit_code: int, output_lines: list[str], during_launch: bool) -> str:
    heading = "The local server failed during launch." if during_launch else "The local server stopped unexpectedly."
    ansi_escape = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    useful = [ansi_escape.sub("", line).strip() for line in output_lines if line.strip()]
    detail = "\n".join(useful[-10:])
    if len(detail) > 1800:
        detail = detail[-1800:]
    message = f"{heading}\n\nExit code: {exit_code}"
    if detail:
        message += f"\n\nLast server messages:\n{detail}"
    message += f"\n\nFull log:\n{_server_log_file()}"
    return message


def _open_url(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(
            ["/usr/bin/open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        webbrowser.open(url)


def _history_file() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    return base / "EPUMapperReview" / "launcher_history.json"


def _server_log_file() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs"
    else:
        base = Path(os.environ.get("LOCALAPPDATA", os.environ.get("APPDATA", str(Path.home()))))
    return base / "EPUMapper" / "server.log"


def _suggest_atlas_root(session_path: str) -> Path | None:
    """Find the Atlas directory belonging to the selected EPU session."""
    if not session_path:
        return None
    try:
        current = Path(session_path).expanduser().resolve()
    except Exception:
        return None
    search_roots = [current, *list(current.parents)[:5]]
    seen: set[Path] = set()
    for root in search_roots:
        candidates = [root] if root.name.lower() == "atlas" else [root / "Atlas", root / "atlas"]
        for candidate in candidates:
            if candidate in seen or not candidate.is_dir():
                continue
            seen.add(candidate)
            patterns = ("Atlas_*.jpg", "Atlas_*.jpeg", "Atlas_*.png", "atlas_*.jpg", "atlas_*.png")
            if any(next(candidate.glob(pattern), None) is not None for pattern in patterns):
                return candidate
    return None


def _dialog_initial_directory(value: str | None) -> Path:
    """Return the closest existing directory for a file/folder dialog."""
    candidate = Path(value).expanduser() if value else Path.home()
    if candidate.is_file():
        return candidate.parent
    if candidate.is_dir():
        return candidate
    for parent in candidate.parents:
        if parent.is_dir():
            return parent
    return Path.home()


def _default_python() -> str:
    return sys.executable or "python"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _runtime_cwd() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return REPO_ROOT


def _ensure_src_path() -> None:
    for runtime_path in (REPO_ROOT / "src", REPO_ROOT):
        if runtime_path.is_dir() and str(runtime_path) not in sys.path:
            sys.path.insert(0, str(runtime_path))


def _review_command(
    session_path: str,
    host: str,
    port: str,
    atlas_path: str,
    atlas_overlay: bool,
    overlay_enabled: bool,
    skip_foil_processing: bool,
    transform: str,
    *,
    session_label: str | None = None,
    details_only: bool = False,
    details_output: str | None = None,
    open_browser: bool = True,
) -> list[str]:
    if _is_frozen():
        cmd = [sys.executable, "--run-review", session_path]
    else:
        cmd = [_default_python(), str(SCRIPT_PATH), "--run-review", session_path]
    cmd.extend(["--host", host, "--port", port, "--overlay-transform", transform])
    if atlas_path:
        cmd.extend(["--atlas", atlas_path])
    if atlas_overlay:
        cmd.append("--atlas-overlay")
    else:
        cmd.append("--no-atlas-overlay")
    if overlay_enabled:
        cmd.append("--overlay")
    else:
        cmd.append("--no-overlay")
    if skip_foil_processing:
        cmd.append("--skip-foil-processing")
    if session_label:
        cmd.extend(["--session-label", session_label])
    if details_only:
        cmd.append("--details-only")
        if details_output:
            cmd.extend(["--details-output", details_output])
    elif open_browser:
        cmd.append("--open")
    return cmd


def _run_review_app(review_args: list[str]) -> int:
    _ensure_src_path()
    try:
        from review_app import main as review_main
    except Exception as exc:
        print(f"[launcher] Failed to import review app: {exc}", file=sys.stderr)
        return 2
    old_argv = sys.argv[:]
    sys.argv = ["review_app.py", *review_args]
    try:
        review_main()
        return 0
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 0
    finally:
        sys.argv = old_argv


def _run_frozen_smoke_test() -> int:
    """Verify that the packaged Windows executable contains its runtime modules."""
    _ensure_src_path()
    try:
        from build_collage import find_grid_image  # noqa: F401
        from portable_session import export_portable_session as portable_export  # noqa: F401
        from review_app import create_app  # noqa: F401
        from scripts.plot_foilhole_positions import compute_markers  # noqa: F401

        if tk is None or ttk is None:
            raise RuntimeError("Tkinter is unavailable")
        if not all(callable(value) for value in (find_grid_image, portable_export, create_app, compute_markers)):
            raise RuntimeError("A packaged runtime entry point is not callable")
    except Exception as exc:
        print(f"[launcher] Windows smoke test failed: {exc}", file=sys.stderr)
        return 2
    return 0


class ReviewLauncher:
    def __init__(self) -> None:
        if tk is None or ttk is None or messagebox is None or filedialog is None:
            raise RuntimeError(
                "Tkinter is not available in this Python environment. "
                "Use the packaged Windows installer/exe, or install Tk support."
            )
        self.proc: subprocess.Popen[str] | None = None
        self.startup_server: ThreadingHTTPServer | None = None
        self.server_ready = False
        self.stop_requested = False
        self.startup_error_state: dict[str, str | None] = {"message": None}
        self.preferences = self._load_preferences()
        self.session_history = list(self.preferences.get("sessions", []))
        saved_atlas_by_session = self.preferences.get("atlas_by_session", {})
        self.atlas_by_session = dict(saved_atlas_by_session) if isinstance(saved_atlas_by_session, dict) else {}
        self._details_running = False
        self._portable_running = False
        self.last_portable_manifest = str(self.preferences.get("last_portable_manifest", "") or "")
        self.root = tk.Tk()
        self.root.title("EPU Mapper")
        self._build_form()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_form(self) -> None:
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        ttk.Label(frm, text="EPU session output folder:").grid(row=0, column=0, sticky="w")
        self.session_var = tk.StringVar(value=self.preferences.get("last_session", ""))
        session_entry = ttk.Entry(frm, textvariable=self.session_var, width=70)
        session_entry.grid(row=1, column=0, sticky="we")
        session_entry.bind("<FocusOut>", lambda _event: self._apply_session_atlas(self.session_var.get().strip()))
        ttk.Button(frm, text="Browse", command=self.browse_session).grid(row=1, column=1, padx=(6, 0))

        ttk.Label(frm, text="Recent sessions:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.recent_var = tk.StringVar()
        self.recent_combo = ttk.Combobox(frm, textvariable=self.recent_var, state="readonly", values=self.session_history)
        self.recent_combo.grid(row=3, column=0, sticky="we")
        self.recent_combo.bind("<<ComboboxSelected>>", self._select_recent_session)

        ttk.Label(frm, text="Atlas mode:").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.atlas_mode_var = tk.StringVar(value=self.preferences.get("atlas_mode", ATLAS_MODE_EPU))
        if self.atlas_mode_var.get() not in (ATLAS_MODE_EPU, ATLAS_MODE_STATIC):
            self.atlas_mode_var.set(ATLAS_MODE_EPU)
        atlas_mode_frame = ttk.Frame(frm)
        atlas_mode_frame.grid(row=5, column=0, columnspan=2, sticky="w")
        for idx, (label, value) in enumerate(ATLAS_MODE_OPTIONS):
            ttk.Radiobutton(
                atlas_mode_frame,
                text=label,
                value=value,
                variable=self.atlas_mode_var,
                command=self._on_atlas_mode_change,
            ).grid(row=idx, column=0, sticky="w", pady=(0 if idx == 0 else 2, 0))

        self.atlas_root_var = tk.StringVar(
            value=self.preferences.get("last_atlas_root", self.preferences.get("last_atlas", ""))
        )
        self.atlas_file_var = tk.StringVar(value=self.preferences.get("last_atlas_file", ""))
        self.atlas_var = tk.StringVar()
        self.atlas_label_text = tk.StringVar()
        self.atlas_label = ttk.Label(frm, textvariable=self.atlas_label_text)
        self.atlas_label.grid(row=6, column=0, sticky="w", pady=(10, 0))
        atlas_entry = ttk.Entry(frm, textvariable=self.atlas_var, width=70)
        atlas_entry.grid(row=7, column=0, sticky="we")
        self.atlas_browse_btn = ttk.Button(frm, text="Browse", command=self.browse_atlas)
        self.atlas_browse_btn.grid(row=7, column=1, padx=(6, 0))
        self._on_atlas_mode_change(remember_current=False)
        self._apply_session_atlas(self.session_var.get().strip())

        ttk.Label(frm, text="Session/Grid label (optional):").grid(row=8, column=0, sticky="w", pady=(10, 0))
        saved_label = str(self.preferences.get("session_label", DEFAULT_LABEL) or "")
        if "\n" in saved_label or "\r" in saved_label or len(saved_label) > 80:
            saved_label = ""
        self.label_var = tk.StringVar(value=saved_label)
        ttk.Entry(frm, textvariable=self.label_var, width=40).grid(row=9, column=0, sticky="we")

        options_row = ttk.Frame(frm)
        options_row.grid(row=10, column=0, columnspan=2, pady=(10, 0), sticky="we")
        ttk.Label(options_row, text="Host:").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value=self.preferences.get("host", DEFAULT_HOST))
        ttk.Entry(options_row, textvariable=self.host_var, width=12).grid(row=0, column=1, padx=(4, 12))
        ttk.Label(options_row, text="Port:").grid(row=0, column=2, sticky="w")
        self.port_var = tk.StringVar(value=self.preferences.get("port", DEFAULT_PORT))
        ttk.Entry(options_row, textvariable=self.port_var, width=8).grid(row=0, column=3, padx=(4, 12))
        self.overlay_var = tk.BooleanVar(value=self.preferences.get("overlay", True))
        self.overlay_check = ttk.Checkbutton(options_row, text="Generate foil overlays", variable=self.overlay_var)
        self.overlay_check.grid(row=0, column=4)
        self.skip_foil_processing_var = tk.BooleanVar(value=self.preferences.get("skip_foil_processing", False))
        ttk.Checkbutton(
            frm,
            text="Atlas/GridSquare only (skip FoilHole processing)",
            variable=self.skip_foil_processing_var,
            command=self._sync_foil_controls,
        ).grid(row=11, column=0, sticky="w", pady=(8, 0))

        self.advanced_var = tk.BooleanVar(value=bool(self.preferences.get("show_advanced", False)))
        ttk.Checkbutton(
            frm,
            text="Show advanced settings",
            variable=self.advanced_var,
            command=self._toggle_advanced,
        ).grid(row=12, column=0, sticky="w", pady=(10, 0))

        self.advanced_frame = ttk.Frame(frm)
        self.advanced_frame.grid(row=13, column=0, columnspan=2, sticky="we")
        ttk.Label(self.advanced_frame, text="Overlay transform:").grid(row=0, column=0, sticky="w")
        transform_labels = [label for label, _ in TRANSFORM_OPTIONS]
        transform_pref = self._transform_label(self.preferences.get("transform", "identity"))
        self.transform_var = tk.StringVar(value=transform_pref if transform_pref in transform_labels else transform_labels[0])
        transform_box = ttk.Combobox(self.advanced_frame, textvariable=self.transform_var, state="readonly")
        transform_box["values"] = transform_labels
        transform_box.grid(row=1, column=0, sticky="we", pady=(4, 0))
        self._toggle_advanced()

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=14, column=0, columnspan=2, pady=(12, 0), sticky="we")
        self.launch_btn = ttk.Button(btn_row, text="Start review", command=self.start_server)
        self.launch_btn.grid(row=0, column=0, sticky="w")
        ttk.Button(btn_row, text="Stop", command=self.stop_server).grid(row=0, column=1, padx=(10, 0))
        self.details_btn = ttk.Button(btn_row, text="Export detailed PDF without review", command=self.export_details)
        self.details_btn.grid(row=0, column=2, padx=(10, 0))
        self.portable_open_btn = ttk.Button(btn_row, text="Open portable session…", command=self.open_portable_session)
        self.portable_open_btn.grid(row=1, column=0, pady=(9, 0), sticky="w")
        self.portable_export_btn = ttk.Button(
            btn_row,
            text="Export portable session…",
            command=self.export_portable_session,
        )
        self.portable_export_btn.grid(row=1, column=1, columnspan=2, padx=(10, 0), pady=(9, 0), sticky="w")
        ttk.Label(
            frm,
            text="Portable export copies the complete EPU session, Atlas, reviews, and a relocatable .epumap file.",
        ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.server_status_var = tk.StringVar(value="Server stopped")
        self.server_status = tk.Label(
            frm,
            textvariable=self.server_status_var,
            anchor="w",
            justify="left",
            padx=12,
            pady=10,
            bg="#e2e8f0",
            fg="#334155",
            font=("TkDefaultFont", 11, "bold"),
        )
        self.server_status.grid(row=16, column=0, columnspan=2, sticky="we", pady=(12, 0))

        output_frame = ttk.LabelFrame(self.root, text="Server log", padding=6)
        output_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        self.root.rowconfigure(1, weight=1)
        self.log_text = tk.Text(output_frame, height=15, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        self._sync_foil_controls()

    def browse_session(self) -> None:
        initial_dir = _dialog_initial_directory(self.session_var.get().strip() or self.preferences.get("last_session", ""))
        path = filedialog.askdirectory(title="Select EPU session output folder", initialdir=str(initial_dir))
        if path:
            self.session_var.set(path)
            self._apply_session_atlas(path)
            self._remember_session(path)
            self._persist_preferences(self._transform_value(self.transform_var.get()))

    def open_portable_session(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showerror("Server running", "Stop the current review server before opening another session.")
            return
        initial_value = self.last_portable_manifest or self.session_var.get().strip()
        path = filedialog.askopenfilename(
            title="Open portable EPU Mapper session",
            initialdir=str(_dialog_initial_directory(initial_value)),
            filetypes=[("EPU Mapper session", "*.epumap"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            loaded = load_portable_session(Path(path))
        except Exception as exc:
            messagebox.showerror("Portable session could not be opened", str(exc), parent=self.root)
            return
        session_path = str(loaded["session_path"])
        atlas_path = str(loaded["atlas_path"] or "")
        atlas_mode = str(loaded.get("atlas_mode", ATLAS_MODE_EPU))
        if atlas_mode not in (ATLAS_MODE_EPU, ATLAS_MODE_STATIC):
            atlas_mode = ATLAS_MODE_EPU
        self.session_var.set(session_path)
        self.atlas_mode_var.set(atlas_mode)
        if atlas_mode == ATLAS_MODE_EPU:
            self.atlas_root_var.set(atlas_path)
        else:
            self.atlas_file_var.set(atlas_path)
        self._on_atlas_mode_change(remember_current=False)
        self.atlas_var.set(atlas_path)
        label = str(loaded.get("session_label", "") or "")
        if len(label) <= 80 and "\n" not in label and "\r" not in label:
            self.label_var.set(label)
        options = loaded.get("options", {})
        self.overlay_var.set(bool(options.get("overlay", True)))
        self.skip_foil_processing_var.set(bool(options.get("skip_foil_processing", False)))
        self.transform_var.set(self._transform_label(str(options.get("transform", "identity"))))
        self._sync_foil_controls()
        self.last_portable_manifest = str(Path(path).resolve())
        self._remember_session(session_path)
        if atlas_mode == ATLAS_MODE_EPU and atlas_path:
            self.atlas_by_session[str(Path(session_path).expanduser())] = atlas_path
        self._persist_preferences(self._transform_value(self.transform_var.get()))
        self._log(f"Opened portable session: {path}\n")
        messagebox.showinfo(
            "Portable session loaded",
            "The portable session paths are ready. Click Start review to open it.",
            parent=self.root,
        )

    def export_portable_session(self) -> None:
        if self._portable_running:
            messagebox.showinfo("Please wait", "A portable session export is already running.")
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showerror(
                "Stop review first",
                "Stop the review server before exporting so review files cannot change during the copy.",
            )
            return
        session_value = self.session_var.get().strip()
        if not session_value or not Path(session_value).expanduser().is_dir():
            messagebox.showerror("Invalid session", "Select an existing EPU session folder first.")
            return
        self._store_atlas_input()
        atlas_value = self._current_atlas_path()
        if atlas_value and not Path(atlas_value).expanduser().exists():
            messagebox.showerror("Invalid Atlas", "The selected Atlas path does not exist.")
            return
        try:
            label = self._validated_session_label()
        except ValueError as exc:
            messagebox.showerror("Invalid session label", str(exc))
            return
        initial_value = self.last_portable_manifest or session_value
        destination = filedialog.askdirectory(
            title="Choose the parent folder for the portable session",
            initialdir=str(_dialog_initial_directory(initial_value)),
        )
        if not destination:
            return
        session_source = portable_session_source(Path(session_value))
        bundle_name = portable_bundle_name(label, session_source)
        if not messagebox.askyesno(
            "Export complete EPU session?",
            f"This will copy all session and Atlas data into:\n\n{Path(destination) / bundle_name}\n\n"
            "Large sessions can take a long time and require substantial free disk space.",
            parent=self.root,
        ):
            return
        options = {
            "overlay": bool(self.overlay_var.get()),
            "skip_foil_processing": bool(self.skip_foil_processing_var.get()),
            "transform": self._transform_value(self.transform_var.get()),
        }
        self._set_portable_running(True)
        threading.Thread(
            target=self._run_portable_export,
            args=(
                Path(session_value),
                Path(atlas_value) if atlas_value else None,
                self._atlas_mode(),
                Path(destination),
                label,
                options,
            ),
            daemon=True,
        ).start()

    def _run_portable_export(
        self,
        session_path: Path,
        atlas_path: Path | None,
        atlas_mode: str,
        destination: Path,
        label: str,
        options: dict,
    ) -> None:
        try:
            manifest_path = export_portable_session(
                session_path,
                atlas_path,
                atlas_mode,
                destination,
                label,
                options,
                self._log,
            )
        except Exception as exc:
            self._log(f"Portable export failed: {exc}\n")
            self.root.after(
                0,
                lambda message=str(exc): messagebox.showerror(
                    "Portable export failed",
                    message,
                    parent=self.root,
                ),
            )
        else:
            self.last_portable_manifest = str(manifest_path)
            self.root.after(0, lambda: self._persist_preferences(self._transform_value(self.transform_var.get())))
            self.root.after(
                0,
                lambda path=str(manifest_path): messagebox.showinfo(
                    "Portable export complete",
                    f"Portable session created successfully:\n\n{path}",
                    parent=self.root,
                ),
            )
        finally:
            self._set_portable_running(False)

    def _apply_session_atlas(self, session_path: str) -> None:
        if self._atlas_mode() != ATLAS_MODE_EPU or not session_path:
            return
        normalized = str(Path(session_path).expanduser())
        remembered = self.atlas_by_session.get(normalized, "")
        if remembered and Path(remembered).is_dir():
            atlas_root = Path(remembered)
        else:
            atlas_root = _suggest_atlas_root(normalized)
        value = str(atlas_root) if atlas_root is not None else ""
        self.atlas_root_var.set(value)
        self.atlas_var.set(value)
        if value:
            self.atlas_by_session[normalized] = value

    def _atlas_mode(self) -> str:
        mode = self.atlas_mode_var.get()
        if mode in (ATLAS_MODE_EPU, ATLAS_MODE_STATIC):
            return mode
        return ATLAS_MODE_EPU

    def _store_atlas_input(self) -> None:
        current = self.atlas_var.get().strip()
        if self._atlas_mode() == ATLAS_MODE_EPU:
            self.atlas_root_var.set(current)
        else:
            self.atlas_file_var.set(current)

    def _current_atlas_path(self) -> str:
        return self.atlas_var.get().strip()

    def _on_atlas_mode_change(self, remember_current: bool = True) -> None:
        if remember_current:
            self._store_atlas_input()
        mode = self._atlas_mode()
        if mode == ATLAS_MODE_EPU:
            self.atlas_label_text.set("Atlas root directory (contains Atlas_*.jpg/.dm/.mrc):")
            self.atlas_var.set(self.atlas_root_var.get().strip())
        else:
            self.atlas_label_text.set("Atlas screenshot file (JPG/PNG):")
            self.atlas_var.set(self.atlas_file_var.get().strip())

    def _toggle_advanced(self) -> None:
        if self.advanced_var.get():
            self.advanced_frame.grid()
        else:
            self.advanced_frame.grid_remove()

    def _sync_foil_controls(self) -> None:
        state = "disabled" if self.skip_foil_processing_var.get() else "normal"
        self.overlay_check.configure(state=state)

    def browse_atlas(self) -> None:
        current = self.atlas_var.get().strip()
        initial_dir = _dialog_initial_directory(current)
        if self._atlas_mode() == ATLAS_MODE_EPU:
            path = filedialog.askdirectory(title="Select atlas root directory", initialdir=str(initial_dir))
        else:
            path = filedialog.askopenfilename(
                title="Select atlas screenshot",
                initialdir=str(initial_dir),
                filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All", "*.*")],
            )
        if path:
            self.atlas_var.set(path)
            self._store_atlas_input()
            if self._atlas_mode() == ATLAS_MODE_EPU and self.session_var.get().strip():
                self.atlas_by_session[str(Path(self.session_var.get().strip()).expanduser())] = path
            self._persist_preferences(self._transform_value(self.transform_var.get()))

    def start_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Already running", "The review app is already running.")
            return
        session_path = self.session_var.get().strip()
        if not session_path:
            messagebox.showerror("Missing path", "Please select the EPU session output folder.")
            return
        if not Path(session_path).exists():
            messagebox.showerror("Invalid path", "The selected EPU session output folder does not exist.")
            return
        self._store_atlas_input()
        atlas_path = self._current_atlas_path()
        atlas_mode = self._atlas_mode()
        atlas_overlay = atlas_mode == ATLAS_MODE_EPU
        if atlas_path:
            atlas_candidate = Path(atlas_path)
            if atlas_mode == ATLAS_MODE_EPU and not atlas_candidate.is_dir():
                messagebox.showerror("Invalid atlas path", "In EPU atlas mode, please choose the atlas root directory.")
                return
            if atlas_mode == ATLAS_MODE_STATIC and not atlas_candidate.is_file():
                messagebox.showerror("Invalid atlas path", "In screenshot mode, please choose an atlas image file.")
                return
        host = self.host_var.get().strip() or DEFAULT_HOST
        port = self.port_var.get().strip() or DEFAULT_PORT
        transform_value = self.transform_var.get()
        transform = self._transform_value(transform_value)

        try:
            label = self._validated_session_label()
        except ValueError as exc:
            messagebox.showerror("Invalid session label", str(exc))
            return
        cmd = _review_command(
            session_path,
            host,
            port,
            atlas_path,
            atlas_overlay,
            self.overlay_var.get(),
            self.skip_foil_processing_var.get(),
            transform,
            session_label=label or None,
            open_browser=False,
        )

        env = self._build_env()

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=_runtime_cwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        except Exception as exc:
            self._set_server_status("error", f"Launch failed: {exc}")
            messagebox.showerror("Failed to launch", f"Could not start review_app: {exc}")
            return
        self.server_ready = False
        self.stop_requested = False
        self.startup_error_state = {"message": None}
        self.log_text.configure(background="#ffffff")
        self._set_server_status("starting", "Starting server - reading session and Atlas data…")
        self._remember_session(session_path)
        self._persist_preferences(transform)
        self.launch_btn.configure(state="disabled")
        threading.Thread(target=self._stream_output, daemon=True).start()
        self._log(f"Persistent server log: {_server_log_file()}\n")
        try:
            self._stop_startup_server()
            self.startup_server, wait_url = _start_browser_wait_page(
                self.proc, host, port, self.startup_error_state
            )
            _open_url(wait_url)
            self._log(f"Preparing the session for {host}:{port}; opened a browser waiting page.\n")
        except Exception as exc:
            self._log(f"Could not open the browser waiting page: {exc}\n")

    def export_details(self) -> None:
        if self._details_running:
            messagebox.showinfo("Please wait", "Detailed export already in progress.")
            return
        session_path = self.session_var.get().strip()
        if not session_path:
            messagebox.showerror("Missing path", "Please select the EPU session output folder.")
            return
        if not Path(session_path).exists():
            messagebox.showerror("Invalid path", "The selected EPU session output folder does not exist.")
            return
        self._store_atlas_input()
        atlas_path = self._current_atlas_path()
        atlas_mode = self._atlas_mode()
        atlas_overlay = atlas_mode == ATLAS_MODE_EPU
        if atlas_path:
            atlas_candidate = Path(atlas_path)
            if atlas_mode == ATLAS_MODE_EPU and not atlas_candidate.is_dir():
                messagebox.showerror("Invalid atlas path", "In EPU atlas mode, please choose the atlas root directory.")
                return
            if atlas_mode == ATLAS_MODE_STATIC and not atlas_candidate.is_file():
                messagebox.showerror("Invalid atlas path", "In screenshot mode, please choose an atlas image file.")
                return
        transform_value = self.transform_var.get()
        transform = self._transform_value(transform_value)
        host = self.host_var.get().strip() or DEFAULT_HOST
        port = self.port_var.get().strip() or DEFAULT_PORT
        try:
            label = self._validated_session_label()
        except ValueError as exc:
            messagebox.showerror("Invalid session label", str(exc))
            return
        cmd = _review_command(
            session_path,
            host,
            port,
            atlas_path,
            atlas_overlay,
            self.overlay_var.get(),
            self.skip_foil_processing_var.get(),
            transform,
            session_label=label or None,
            details_only=True,
            open_browser=False,
        )
        self._set_details_running(True)
        threading.Thread(
            target=self._run_details_job,
            args=(cmd, session_path, transform),
            daemon=True,
        ).start()

    def stop_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.stop_requested = True
            self.proc.terminate()
            self._log("Stopping server...\n")
        self.proc = None
        self._stop_startup_server()
        self.launch_btn.configure(state="normal")
        self._set_server_status("stopped", "Server stopped")

    def _set_server_status(self, state: str, message: str) -> None:
        colors = {
            "stopped": ("#e2e8f0", "#334155"),
            "starting": ("#dbeafe", "#1e40af"),
            "running": ("#d1fae5", "#065f46"),
            "error": ("#ffe4e6", "#9f1239"),
        }
        background, foreground = colors.get(state, colors["stopped"])
        self.server_status_var.set(message)
        self.server_status.configure(bg=background, fg=foreground)

    def _show_server_error(self, message: str, during_launch: bool) -> None:
        title = "Server launch failed" if during_launch else "Server stopped unexpectedly"
        first_line = message.splitlines()[0] if message else title
        self._set_server_status("error", f"⚠ {first_line} See the dialog and server log below.")
        self.log_text.configure(background="#fff1f2")
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(500, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        messagebox.showerror(title, message, parent=self.root)

    def _validated_session_label(self) -> str:
        label = self.label_var.get().strip()
        if "\n" in label or "\r" in label:
            raise ValueError("The optional session label must be a single line.")
        if len(label) > 80:
            raise ValueError("The optional session label must be 80 characters or fewer.")
        return label

    def _stop_startup_server(self) -> None:
        server = self.startup_server
        self.startup_server = None
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def _transform_value(self, label: str) -> str:
        for text, value in TRANSFORM_OPTIONS:
            if text == label:
                return value
        return "identity"

    def _transform_label(self, value: str) -> str:
        for text, val in TRANSFORM_OPTIONS:
            if val == value:
                return text
        return TRANSFORM_OPTIONS[0][0]

    def _select_recent_session(self, _event: tk.Event) -> None:
        val = self.recent_var.get()
        if val:
            self.session_var.set(val)
            self._apply_session_atlas(val)

    def _prefs_path(self) -> Path:
        return _history_file()

    def _load_preferences(self) -> dict:
        path = self._prefs_path()
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _remember_session(self, session_path: str) -> None:
        norm = str(Path(session_path))
        if norm in self.session_history:
            self.session_history.remove(norm)
        self.session_history.insert(0, norm)
        self.session_history = self.session_history[:5]
        self.recent_combo["values"] = self.session_history

    def _persist_preferences(self, transform: str) -> None:
        self._store_atlas_input()
        prefs = {
            "sessions": self.session_history,
            "host": self.host_var.get().strip() or DEFAULT_HOST,
            "port": self.port_var.get().strip() or DEFAULT_PORT,
            "transform": transform,
            "overlay": bool(self.overlay_var.get()),
            "skip_foil_processing": bool(self.skip_foil_processing_var.get()),
            "last_session": self.session_var.get().strip(),
            "atlas_mode": self._atlas_mode(),
            "last_atlas_root": self.atlas_root_var.get().strip(),
            "last_atlas_file": self.atlas_file_var.get().strip(),
            "last_atlas": self._current_atlas_path(),
            "atlas_by_session": self.atlas_by_session,
            "session_label": self.label_var.get().strip(),
            "show_advanced": bool(self.advanced_var.get()),
            "last_portable_manifest": self.last_portable_manifest,
        }
        path = self._prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs, indent=2))

    def _build_env(self) -> dict:
        env = os.environ.copy()
        src_dir = REPO_ROOT / "src"
        if src_dir.is_dir():
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(src_dir) if not existing else f"{src_dir}{os.pathsep}{existing}"
        temp_dir = env.get("TMP", env.get("TEMP", os.path.expanduser("~")))
        env.setdefault("MPLCONFIGDIR", os.path.join(temp_dir, "mplcache"))
        env.setdefault("FONTCONFIG_PATH", os.path.join(temp_dir, "mplcache"))
        return env

    def _stream_output(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        output_tail: deque[str] = deque(maxlen=30)
        log_path = _server_log_file()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
        except OSError:
            log_handle = None
        header = f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting EPU Mapper server (PID {proc.pid})\n"
        if log_handle:
            log_handle.write(header)
            log_handle.flush()
        for line in proc.stdout:
            output_tail.append(line.rstrip())
            self._log(line)
            if not self.server_ready and line.strip():
                clean_line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
                self.startup_error_state["message"] = (
                    "EPU Mapper stopped before the server was ready.\n\n"
                    f"Last server message: {clean_line}\n\n"
                    "Return to the launcher for the full error log."
                )
            if not self.server_ready and ("Uvicorn running on" in line or "Application startup complete" in line):
                self.server_ready = True
                self.root.after(0, lambda: self._set_server_status("running", "Server running - dashboard is ready"))
            if log_handle:
                log_handle.write(line)
                log_handle.flush()
        exit_code = proc.wait()
        exit_line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Server exited with code {exit_code}.\n"
        self._log(exit_line)
        if log_handle:
            log_handle.write(exit_line)
            log_handle.close()
        if self.proc is proc:
            self.proc = None
        during_launch = not self.server_ready
        if not self.stop_requested:
            failure_message = _server_failure_message(exit_code, list(output_tail), during_launch)
            self.startup_error_state["message"] = failure_message
            self.root.after(
                0,
                lambda message=failure_message, startup=during_launch: self._show_server_error(message, startup),
            )
        else:
            self.root.after(0, lambda: self._set_server_status("stopped", "Server stopped"))
        self.root.after(0, lambda: self.launch_btn.configure(state="normal"))

    def _run_details_job(self, cmd: list[str], session_path: str, transform: str) -> None:
        env = self._build_env()
        self._log("Generating detailed PDF for all GridSquares without interactive review…\n")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=_runtime_cwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        except Exception as exc:
            self._log(f"Failed to start export: {exc}\n")
            self.root.after(0, lambda: messagebox.showerror("Export failed", f"Could not start review_app: {exc}"))
            self._set_details_running(False)
            return
        assert proc.stdout
        for line in proc.stdout:
            self._log(line)
        ret = proc.wait()
        if ret == 0:
            self._log("Detailed PDF export finished.\n")
            self.root.after(0, lambda: self._remember_session(session_path))
            self.root.after(0, lambda: self._persist_preferences(transform))
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "Export complete",
                    "Detailed PDF generated successfully without interactive review.",
                ),
            )
        else:
            self._log(f"Detailed export failed (exit code {ret}).\n")
            self.root.after(0, lambda: messagebox.showerror("Export failed", f"review_app exited with code {ret}"))
        self._set_details_running(False)

    def _set_details_running(self, running: bool) -> None:
        self._details_running = running
        def toggle() -> None:
            state = "disabled" if running else "normal"
            self.details_btn.configure(state=state)
        self.root.after(0, toggle)

    def _set_portable_running(self, running: bool) -> None:
        self._portable_running = running

        def toggle() -> None:
            state = "disabled" if running else "normal"
            self.portable_export_btn.configure(state=state, text="Exporting portable session…" if running else "Export portable session…")
            self.portable_open_btn.configure(state=state)

        self.root.after(0, toggle)

    def _log(self, text: str) -> None:
        def append() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, append)

    def on_close(self) -> None:
        if self._portable_running:
            messagebox.showwarning(
                "Portable export running",
                "Wait for the portable session export to finish before closing EPU Mapper.",
                parent=self.root,
            )
            return
        if self.proc and self.proc.poll() is None:
            if messagebox.askyesno("Quit", "Server is still running. Stop it?"):
                self.stop_server()
            else:
                return
        self._stop_startup_server()
        self._persist_preferences(self._transform_value(self.transform_var.get()))
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke-test":
        raise SystemExit(_run_frozen_smoke_test())
    if len(sys.argv) > 1 and sys.argv[1] == "--run-review":
        raise SystemExit(_run_review_app(sys.argv[2:]))
    app = ReviewLauncher()
    app.run()


if __name__ == "__main__":
    main()
