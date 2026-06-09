@echo off
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0service.ps1" %*
