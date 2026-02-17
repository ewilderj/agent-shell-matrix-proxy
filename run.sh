#!/bin/bash
set -e

# Install dependencies if needed
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Create .env if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Edit .env with your Matrix credentials before running!"
    exit 1
fi

# Set up logging
LOG_DIR="$HOME/.matrix-proxy-bot/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/bot.log"

echo "Starting matrix-proxy-bot..."
echo "Logs: $LOG_FILE"
echo ""

# Run with tee to both stdout and log file
uv run -m matrix_proxy_bot 2>&1 | tee "$LOG_FILE"

