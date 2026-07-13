@echo off
if exist "venv\Scripts\activate.bat" (
    start "StreamHome Backend" cmd /k "call venv\Scripts\activate && cd server && python main.py"
) else (
    start "StreamHome Backend" cmd /k "cd server && python main.py"
)
start "StreamHome Frontend" cmd /k "cd web && npm run dev"
