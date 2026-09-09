"""Small, independent launcher for the personal density fork."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

SETTINGS = Path.home() / '.epumapper-density-launcher.json'


def read_settings():
    try:
        value = json.loads(SETTINGS.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def launch(atlas=None):
    root = tk.Tk()
    root.title('EPU Mapper · Particle density experiment')
    root.geometry('860x550')
    saved = read_settings()
    source = tk.StringVar(value=saved.get('source', ''))
    atlas_path = tk.StringVar(value=str(atlas or saved.get('atlas', '')))
    status = tk.StringVar(value='Choose the EPU session and optional atlas, then start.')
    events = queue.Queue()
    state = dict(process=None, url=None, stopping=False)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(0, weight=1)

    def browse(variable, image=False):
        current = Path(variable.get()).expanduser() if variable.get() else Path.home()
        initial = current if current.is_dir() else current.parent
        options = dict(parent=root, initialdir=str(initial))
        value = (filedialog.askopenfilename(title='Select Atlas preview', filetypes=[('Atlas preview', '*.jpg *.jpeg *.png')], **options)
                 if image else filedialog.askdirectory(title='Select folder', **options))
        if value:
            variable.set(value)

    ttk.Label(frame, text='EPU session folder (or Images-Disc1)').grid(row=0, column=0, sticky='w')
    ttk.Entry(frame, textvariable=source).grid(row=1, column=0, sticky='ew', pady=6)
    ttk.Button(frame, text='Browse…', command=lambda: browse(source)).grid(row=1, column=1, padx=6)
    ttk.Label(frame, text='Atlas folder (contains Atlas*.jpg / PNG and Atlas.dm), or preview file').grid(row=2, column=0, sticky='w')
    ttk.Entry(frame, textvariable=atlas_path).grid(row=3, column=0, sticky='ew', pady=6)
    ttk.Button(frame, text='Browse folder…', command=lambda: browse(atlas_path)).grid(row=3, column=1, padx=6)
    ttk.Button(frame, text='Choose image…', command=lambda: browse(atlas_path, True)).grid(row=4, column=1)
    ttk.Label(frame, text='Atlas is optional. Keep its metadata beside the preview for mapped square positions.').grid(row=4, column=0, sticky='w')
    actions = ttk.Frame(frame)
    actions.grid(row=5, column=0, columnspan=2, sticky='w', pady=14)
    ttk.Label(frame, textvariable=status, wraplength=800).grid(row=6, column=0, columnspan=2, sticky='w', pady=8)
    log = tk.Text(frame, height=14, wrap='word', state='disabled')
    log.grid(row=7, column=0, columnspan=2, sticky='nsew')
    frame.rowconfigure(7, weight=1)

    def start():
        if state['process'] and state['process'].poll() is None:
            return
        src = Path(source.get()).expanduser()
        atl = Path(atlas_path.get()).expanduser() if atlas_path.get().strip() else None
        if not source.get().strip() or not src.is_dir() or (atl and not atl.exists()):
            messagebox.showerror('Folder unavailable', 'Check the session and Atlas paths. Reconnect the network share if needed.', parent=root)
            return
        command = [sys.executable, '-u', str(Path(__file__).with_name('app.py')), str(src), '--no-browser']
        if atl:
            command += ['--atlas', str(atl)]
        try:
            SETTINGS.write_text(json.dumps(dict(source=str(src), atlas=str(atl or ''))))
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            messagebox.showerror('Cannot start mapper', str(exc), parent=root)
            return
        state.update(process=process, url=None, stopping=False)
        start_button.config(state='disabled')
        open_button.config(state='disabled')
        status.set('Starting server…')
        def read_output():
            with process.stdout:
                for line in process.stdout:
                    events.put(('line', line))
            events.put(('exit', process.wait()))
        threading.Thread(target=read_output, daemon=True).start()

    def stop():
        process = state['process']
        if process and process.poll() is None:
            state['stopping'] = True
            status.set('Stopping server…')
            process.terminate()

    def poll():
        while not events.empty():
            kind, value = events.get_nowait()
            if kind == 'line':
                log.config(state='normal'); log.insert('end', value); log.see('end'); log.config(state='disabled')
                if value.startswith('[epumapper-ready] '):
                    state['url'] = json.loads(value.split(' ', 1)[1])['url']
                    status.set('Dashboard ready: ' + state['url'])
                    open_button.config(state='normal')
                    webbrowser.open(state['url'])
            else:
                start_button.config(state='normal'); open_button.config(state='disabled')
                if not state['stopping']:
                    status.set(f'Server stopped unexpectedly (exit {value}). See messages below.')
                    messagebox.showerror('Density mapper server stopped', status.get() + '\n\n' + log.get('end-18l', 'end'), parent=root)
                else:
                    status.set('Server stopped. You can select another session or atlas.')
        root.after(150, poll)

    def close():
        if state['process'] and state['process'].poll() is None:
            if not messagebox.askyesno('Close mapper?', 'Stop the experimental server and close the launcher?', parent=root):
                return
            stop()
        root.destroy()

    start_button = ttk.Button(actions, text='Start density mapper', command=start)
    start_button.pack(side='left', padx=4)
    ttk.Button(actions, text='Stop', command=stop).pack(side='left', padx=4)
    open_button = ttk.Button(actions, text='Open dashboard', command=lambda: webbrowser.open(state['url']), state='disabled')
    open_button.pack(side='left', padx=4)
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(150, poll)
    root.mainloop()
