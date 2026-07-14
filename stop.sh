#!/bin/bash
echo "===================================================="
echo "Stopping StreamHome backend and frontend processes..."
echo "===================================================="
pkill -f "main.py" >/dev/null 2>&1
pkill -f "tsx server.ts" >/dev/null 2>&1
pkill -f "npm run dev" >/dev/null 2>&1

# 2. Port-based fallback killing (ensures ports 8000, 3000, 24678 are completely freed)
if command -v fuser >/dev/null 2>&1; then
    fuser -k 8000/tcp >/dev/null 2>&1
    fuser -k 3000/tcp >/dev/null 2>&1
    fuser -k 24678/tcp >/dev/null 2>&1
else
    # Fallback to lsof/kill if fuser is missing
    for port in 8000 3000 24678; do
        pid=$(lsof -t -i:$port 2>/dev/null)
        if [ -n "$pid" ]; then
            kill -9 $pid >/dev/null 2>&1
        fi
    done
fi

echo "Processes terminated."
