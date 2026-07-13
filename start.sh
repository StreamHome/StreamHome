#!/bin/bash
echo "===================================================="
echo "Starting StreamHome Media Server (Interactive Mode)"
echo "===================================================="
echo ""

# Activate virtual environment if present
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

echo "Starting FastAPI Server..."
cd server && python3 main.py &
SERVER_PID=$!

echo "Starting Vite Dev Server..."
cd ../web && npm run dev &
WEB_PID=$!

echo ""
echo "Both processes started."
echo "Press Ctrl+C to stop both."
echo "===================================================="

# Trap SIGINT (Ctrl+C) and terminate background jobs
trap "kill $SERVER_PID $WEB_PID; exit" SIGINT

wait
