on run
    set pythonPath to "__PYTHON_PATH__"
    set appPath to POSIX path of (path to me)
    set launcherPath to appPath & "Contents/Resources/runtime/scripts/macos_gui_launcher.py"
    set logFolder to (POSIX path of (path to library folder from user domain)) & "Logs"
    set logPath to logFolder & "/EPUMapperLauncher.log"
    set launchCommand to quoted form of pythonPath & " -B " & quoted form of launcherPath & " >> " & quoted form of logPath & " 2>&1 &"
    do shell script "/bin/mkdir -p " & quoted form of logFolder & " && " & launchCommand
end run
