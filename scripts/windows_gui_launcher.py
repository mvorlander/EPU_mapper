#!/usr/bin/env python3
"""Simple Tkinter launcher for the EPU Mapper review app on Windows."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
def _history_file() -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home())))
    return base / "EPUMapperReview" / "launcher_history.json"


def _default_python() -> str:
    return sys.executable or "python"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _runtime_cwd() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return REPO_ROOT


def _ensure_src_path() -> None:
    src_dir = REPO_ROOT / "src"
    if src_dir.is_dir() and str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))


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


class ReviewLauncher:
    def __init__(self) -> None:
        if tk is None or ttk is None or messagebox is None or filedialog is None:
            raise RuntimeError(
                "Tkinter is not available in this Python environment. "
                "Use the packaged Windows installer/exe, or install Tk support."
            )
        self.proc: subprocess.Popen[str] | None = None
        self.preferences = self._load_preferences()
        self.session_history = list(self.preferences.get("sessions", []))
        self._details_running = False
        self.root = tk.Tk()
        self.root.title("EPU Mapper Review Launcher")
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

        ttk.Label(frm, text="Session/Grid label (optional):").grid(row=8, column=0, sticky="w", pady=(10, 0))
        self.label_var = tk.StringVar(value=self.preferences.get("session_label", DEFAULT_LABEL))
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
        ttk.Label(
            frm,
            text="This export runs immediately for all GridSquares and skips the interactive review UI.",
        ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(6, 0))

        output_frame = ttk.LabelFrame(self.root, text="Server log", padding=6)
        output_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        self.root.rowconfigure(1, weight=1)
        self.log_text = tk.Text(output_frame, height=15, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        self._sync_foil_controls()

    def browse_session(self) -> None:
        path = filedialog.askdirectory(title="Select EPU session output folder")
        if path:
            self.session_var.set(path)

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
        if self._atlas_mode() == ATLAS_MODE_EPU:
            path = filedialog.askdirectory(title="Select atlas root directory")
        else:
            path = filedialog.askopenfilename(
                title="Select atlas screenshot",
                filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All", "*.*")],
            )
        if path:
            self.atlas_var.set(path)
            self._store_atlas_input()

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

        label = self.label_var.get().strip()
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
            messagebox.showerror("Failed to launch", f"Could not start review_app: {exc}")
            return
        self._remember_session(session_path)
        self._persist_preferences(transform)
        self.launch_btn.configure(state="disabled")
        threading.Thread(target=self._stream_output, daemon=True).start()
        self._log(f"Started server on {host}:{port}. Close the browser tab when finished.\n")

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
        label = self.label_var.get().strip()
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
            self.proc.terminate()
            self._log("Stopping server...\n")
        self.proc = None
        self.launch_btn.configure(state="normal")

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
            "session_label": self.label_var.get().strip(),
            "show_advanced": bool(self.advanced_var.get()),
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
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._log(line)
        self._log("Server exited.\n")
        self.proc = None
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

    def _log(self, text: str) -> None:
        def append() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, append)

    def on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if messagebox.askyesno("Quit", "Server is still running. Stop it?"):
                self.stop_server()
            else:
                return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--run-review":
        raise SystemExit(_run_review_app(sys.argv[2:]))
    app = ReviewLauncher()
    app.run()


if __name__ == "__main__":
    main()
