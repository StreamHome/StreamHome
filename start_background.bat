@echo off
echo ====================================================
echo Starting StreamHome in Background (Hidden Windows)
echo ====================================================
echo.
echo Launching FastAPI Server in background...
if exist "venv\Scripts\activate.bat" (
    powershell -Command "Start-Process -FilePath cmd.exe -ArgumentList '/c call ..\venv\Scripts\activate && python main.py' -WorkingDirectory 'server' -WindowStyle Hidden"
) else (
    powershell -Command "Start-Process -FilePath python -ArgumentList 'main.py' -WorkingDirectory 'server' -WindowStyle Hidden"
)
echo Launching Vite Dev Server in background...
powershell -Command "Start-Process -FilePath npm -ArgumentList 'run dev' -WorkingDirectory 'web' -WindowStyle Hidden"
echo.
echo Both servers have been launched in the background (hidden).
echo To stop them, close python.exe and node.exe processes in Task Manager,
echo or run stop.bat.
echo ====================================================
pause
