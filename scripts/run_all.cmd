@echo off
rem Launcher that works from cmd.exe, Git Bash, or a double-click in Explorer.
rem (A .ps1 file would just open in Notepad there; this hands it to PowerShell explicitly.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all.ps1"
