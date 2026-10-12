#!/bin/bash
# Start mccia-search in mock mode (no API keys needed)
# Usage: ./start_mock.sh

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Starting MCCIA Search in MOCK MODE..."
echo "No API keys or database required."
echo ""

# Start mock backend
cd "$PROJECT_ROOT/backend"
uv run uvicorn mock_server:app --port 8000 --reload &
BACKEND_PID=$!
echo "Mock backend started on http://localhost:8000 (PID $BACKEND_PID)"

# Wait for backend
sleep 2

# Start frontend
cd "$PROJECT_ROOT/web"
npm run dev &
FRONTEND_PID=$!
echo "Frontend started on http://localhost:3000 (PID $FRONTEND_PID)"

echo ""
echo "================================================"
echo "  MCCIA Search running in MOCK MODE"
echo "  Open: http://localhost:3000"
echo "  Mock backend: http://localhost:8000"
echo "  Press Ctrl+C to stop both servers"
echo "================================================"

# Keep running until Ctrl+C
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Stopped.'" EXIT
wait
