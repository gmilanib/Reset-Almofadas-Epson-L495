@echo off
setlocal
"%~dp0runtime\python\Scripts\python.exe" "%~dp0Reset-L495.py" %*
exit /b %errorlevel%
