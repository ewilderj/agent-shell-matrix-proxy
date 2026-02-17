#!/bin/bash
# Wrapper script to run comprehensive Python test suite
# This script ensures the test can be run from anywhere

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "Running comprehensive test suite..."
echo ""

uv run tests/test_endpoints.py

exit $?
