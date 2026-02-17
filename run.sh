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

# Run
echo "Starting matrix-proxy-bot..."
uv run -m matrix_proxy_bot
