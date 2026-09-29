"""Small native file/folder chooser used by the local dashboard."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _ps_literal(value):
    """PowerShell single-quoted literal (also safe for $, backticks and spaces)."""
    return "'"+str(value or '').replace("'", "''")+"'"


def _mac_dialog(folder, prompt, initial=None):
    chooser = 'choose folder' if folder else 'choose file'
    default = ''
    args = [prompt]
    if initial and Path(initial).exists():
        default = ' default location (POSIX file (item 2 of argv))'
        args.append(str(Path(initial).resolve()))
    script = f'''on run argv
try
set picked to {chooser} with prompt (item 1 of argv){default}
return POSIX path of picked
on error number -128
return ""
end try
end run'''
    result=subprocess.run(['/usr/bin/osascript','-e',script,'--',*args],capture_output=True,text=True,timeout=600,check=False)
    if result.returncode and result.stderr.strip():
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def _windows_dialog(folder, prompt, initial=None):
    initial=str(Path(initial).resolve()) if initial and Path(initial).exists() else ''
    if folder:
        script=("Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.Description={_ps_literal(prompt)};$d.SelectedPath={_ps_literal(initial)};"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.SelectedPath)}")
    else:
        script=("Add-Type -AssemblyName System.Windows.Forms;"
                "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                f"$d.Title={_ps_literal(prompt)};$d.InitialDirectory={_ps_literal(initial)};"
                "$d.Filter='CryoSPARC datasets (*.cs)|*.cs|All files (*.*)|*.*';"
                "if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.FileName)}")
    result=subprocess.run(['powershell','-NoProfile','-STA','-Command',script],capture_output=True,text=True,timeout=600,check=False)
    if result.returncode and result.stderr.strip():raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def choose_path(kind='cs', prompt='', initial=None):
    folder=kind=='folder'
    prompt=prompt or ('Choose output folder' if folder else 'Choose CryoSPARC particle dataset')
    if initial:
        initial=Path(initial).expanduser()
        if initial.is_file():initial=initial.parent
    if sys.platform=='darwin':value=_mac_dialog(folder,prompt,initial)
    elif os.name=='nt':value=_windows_dialog(folder,prompt,initial)
    elif shutil.which('zenity'):
        command=['zenity','--file-selection','--title='+prompt]
        if folder:command.append('--directory')
        if initial:command.append('--filename='+str(initial))
        result=subprocess.run(command,capture_output=True,text=True,timeout=600,check=False)
        value=result.stdout.strip() if result.returncode==0 else ''
    else:
        raise RuntimeError('A native file chooser is unavailable on this system. Use browser upload instead.')
    if not value:return None
    path=Path(value).expanduser().resolve(strict=True)
    if folder and not path.is_dir():raise ValueError('Choose a folder.')
    if not folder and (not path.is_file() or path.suffix.lower()!='.cs'):raise ValueError('Choose a CryoSPARC .cs file.')
    return path
