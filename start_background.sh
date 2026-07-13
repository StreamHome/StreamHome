#!/bin/bash
echo "===================================================="
echo "Starting StreamHome in Background (Detached Mode)"
echo "===================================================="
echo ""
echo "Launching FastAPI Server in background..."
if [ -d "venv" ]; then
    nohup bash -c "source venv/bin/activate && cd server && python3 main.py" > server_boot.log 2>&1 &
else
    nohup bash -c "cd server && python3 main.py" > server_boot.log 2>&1 &
fi
echo "Launching Vite Dev Server in background..."
nohup bash -c "cd web && npm run dev" > web_boot.log 2>&1 &
echo ""
echo "Both servers launched in the background."
echo "Logs are written to server_boot.log and web_boot.log."
echo "To stop them, run ./stop.sh or kill python3/node processes."
echo "===================================================="
