@echo off
rem Compatibility alias: start.bat already launches hidden background processes.
call "%~dp0start.bat" %*
exit /b %errorlevel%
