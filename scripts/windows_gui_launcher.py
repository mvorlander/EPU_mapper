#!/usr/bin/env python3
"""Simple Tkinter launcher for the EPU Mapper review app on Windows."""
from __future__ import annotations

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
    overlay_enabled: bool,
    transform: str,
) -> list[str]:
    if _is_frozen():
        cmd = [sys.executable, "--run-review", session_path]
    else:
        cmd = [_default_python(), str(SCRIPT_PATH), "--run-review", session_path]
    cmd.extend(["--host", host, "--port", port, "--overlay-transform", transform])
    if atlas_path:
        cmd.extend(["--atlas", atlas_path])
    if overlay_enabled:
        cmd.append("--overlay")
    else:
        cmd.append("--no-overlay")
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
        self.root = tk.Tk()
        self.root.title("EPU Mapper Review Launcher")
        self._build_form()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_form(self) -> None:
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        ttk.Label(frm, text="Session root or Images-Disc folder:").grid(row=0, column=0, sticky="w")
        self.session_var = tk.StringVar()
        session_entry = ttk.Entry(frm, textvariable=self.session_var, width=70)
        session_entry.grid(row=1, column=0, sticky="we")
        ttk.Button(frm, text="Browse", command=self.browse_session).grid(row=1, column=1, padx=(6, 0))

        ttk.Label(frm, text="Atlas screenshot (optional but recommended):").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.atlas_var = tk.StringVar()
        atlas_entry = ttk.Entry(frm, textvariable=self.atlas_var, width=70)
        atlas_entry.grid(row=3, column=0, sticky="we")
        ttk.Button(frm, text="Browse", command=self.browse_atlas).grid(row=3, column=1, padx=(6, 0))

        options_row = ttk.Frame(frm)
        options_row.grid(row=4, column=0, columnspan=2, pady=(10, 0), sticky="we")
        ttk.Label(options_row, text="Host:").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        ttk.Entry(options_row, textvariable=self.host_var, width=12).grid(row=0, column=1, padx=(4, 12))
        ttk.Label(options_row, text="Port:").grid(row=0, column=2, sticky="w")
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Entry(options_row, textvariable=self.port_var, width=8).grid(row=0, column=3, padx=(4, 12))
        self.overlay_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_row, text="Generate foil overlays", variable=self.overlay_var).grid(row=0, column=4)

        ttk.Label(frm, text="Overlay transform:").grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.transform_var = tk.StringVar(value="identity")
        transform_box = ttk.Combobox(frm, textvariable=self.transform_var, state="readonly")
        transform_box["values"] = [label for label, _ in TRANSFORM_OPTIONS]
        transform_box.current(0)
        transform_box.grid(row=6, column=0, sticky="we")

        self.launch_btn = ttk.Button(frm, text="Start review", command=self.start_server)
        self.launch_btn.grid(row=7, column=0, pady=(12, 0), sticky="w")
        ttk.Button(frm, text="Stop", command=self.stop_server).grid(row=7, column=1, pady=(12, 0), sticky="e")

        output_frame = ttk.LabelFrame(self.root, text="Server log", padding=6)
        output_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        self.root.rowconfigure(1, weight=1)
        self.log_text = tk.Text(output_frame, height=15, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def browse_session(self) -> None:
        path = filedialog.askdirectory(title="Select session root or Images-Disc folder")
        if path:
            self.session_var.set(path)

    def browse_atlas(self) -> None:
        path = filedialog.askopenfilename(title="Select atlas screenshot", filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All", "*.*")])
        if path:
            self.atlas_var.set(path)

    def start_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Already running", "The review app is already running.")
            return
        session_path = self.session_var.get().strip()
        if not session_path:
            messagebox.showerror("Missing path", "Please select a session root or Images-Disc folder.")
            return
        if not Path(session_path).exists():
            messagebox.showerror("Invalid path", "The selected session path does not exist.")
            return
        atlas_path = self.atlas_var.get().strip()
        host = self.host_var.get().strip() or DEFAULT_HOST
        port = self.port_var.get().strip() or DEFAULT_PORT
        transform_value = self.transform_var.get()
        transform = next((value for label, value in TRANSFORM_OPTIONS if label == transform_value), "identity")

        cmd = _review_command(session_path, host, port, atlas_path, self.overlay_var.get(), transform)

        env = os.environ.copy()
        src_dir = REPO_ROOT / "src"
        if src_dir.is_dir():
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(src_dir) if not existing else f"{src_dir}{os.pathsep}{existing}"
        temp_dir = env.get("TMP", env.get("TEMP", os.path.expanduser("~")))
        env.setdefault("MPLCONFIGDIR", os.path.join(temp_dir, "mplcache"))
        env.setdefault("FONTCONFIG_PATH", os.path.join(temp_dir, "mplcache"))

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
        self.launch_btn.configure(state="disabled")
        threading.Thread(target=self._stream_output, daemon=True).start()
        self._log(f"Started server on {host}:{port}. Close the browser tab when finished.\n")

    def stop_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._log("Stopping server...\n")
        self.proc = None
        self.launch_btn.configure(state="normal")

    def _stream_output(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._log(line)
        self._log("Server exited.\n")
        self.proc = None
        self.root.after(0, lambda: self.launch_btn.configure(state="normal"))

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
