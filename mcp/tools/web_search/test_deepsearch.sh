#!/usr/bin/env bash

set -Eeuo pipefail

# ==========================================================
# DEEPSEARCH - Complete CLI Test Runner
# ==========================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-reader-lm:1.5b}"

# Test configuration (can be overridden through environment variables)
TOPIC="${1:-Autonomous Vehicles and Self-Driving Technology}"
DEPTH="${DEPTH:-2}"
URLS="${URLS:-30}"
PAGES="${PAGES:-20}"
QUERIES="${QUERIES:-15}"
DURATION="${DURATION:-600}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/deepsearch_test_$TIMESTAMP.log"

mkdir -p "$LOG_DIR"

# ==========================================================
# Logging
# ==========================================================

exec > >(tee -a "$LOG_FILE") 2>&1

section() {
    echo
    echo "=========================================================="
    echo " $1"
    echo "=========================================================="
}

fail() {
    echo
    echo "[FAIL] $1"
    echo "[INFO] Full log: $LOG_FILE"
    exit 1
}

# ==========================================================
# 1. Check project files
# ==========================================================

section "1. PROJECT CHECK"

if [[ ! -f "$SCRIPT_DIR/web_search.py" ]]; then
    fail "web_search.py not found in $SCRIPT_DIR"
fi

echo "[OK] web_search.py found"
echo "[INFO] Project directory: $SCRIPT_DIR"

# ==========================================================
# 2. Python environment
# ==========================================================

section "2. PYTHON ENVIRONMENT"

if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
    echo "[OK] Virtual environment detected"
fi

echo "[INFO] Python executable: $PYTHON_BIN"

"$PYTHON_BIN" --version || fail "Python is not working"

# ==========================================================
# 3. Dependency check
# ==========================================================

section "3. PYTHON DEPENDENCIES"

"$PYTHON_BIN" - <<'PY'
import importlib.util
import sys

packages = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "bs4": "beautifulsoup4",
    "trafilatura": "trafilatura",
    "pydantic": "pydantic",
}

missing = []

for module, package in packages.items():
    if importlib.util.find_spec(module):
        print(f"[OK] {package}")
    else:
        print(f"[MISSING] {package}")
        missing.append(package)

if missing:
    print("\nInstall missing dependencies:")
    print("pip install " + " ".join(missing))
    sys.exit(1)
PY

[[ $? -eq 0 ]] || fail "Python dependency check failed"

# ==========================================================
# 4. Ollama check
# ==========================================================

# ==========================================================
# Check Ollama model using its API
# ==========================================================

section "4. OLLAMA MODEL VALIDATION"

if ! curl -fsS "$OLLAMA_URL/api/tags" |
    "$PYTHON_BIN" -c '
import json
import sys

target = sys.argv[1]
data = json.load(sys.stdin)

models = data.get("models", [])

for model in models:
    name = model.get("name", "")
    print(f"[INFO] Available model: {name}")

    if name == target:
        print(f"[OK] Model found: {target}")
        sys.exit(0)

print(f"[ERROR] Model not found: {target}")
sys.exit(1)
' "$OLLAMA_MODEL"; then

    fail "Required Ollama model is missing or API check failed"
fi

# ==========================================================
# 5. CLI argument check
# ==========================================================

section "5. CLI CHECK"

"$PYTHON_BIN" web_search.py --help ||
    fail "CLI help command failed"

"$PYTHON_BIN" web_search.py research --help ||
    fail "Research CLI help command failed"

# ==========================================================
# 6. Actual research test
# ==========================================================

section "6. STARTING DEEPSEARCH RESEARCH"

echo "[INFO] Topic: $TOPIC"
echo "[INFO] Depth: $DEPTH"
echo "[INFO] Maximum URLs: $URLS"
echo "[INFO] Maximum pages: $PAGES"
echo "[INFO] Maximum queries: $QUERIES"
echo "[INFO] Maximum duration: $DURATION seconds"
echo "[INFO] Model: $OLLAMA_MODEL"

echo
echo "[INFO] Starting research..."
echo "[INFO] Progress events and generated Markdown will appear below."
echo

set +e

"$PYTHON_BIN" web_search.py research "$TOPIC" \
    --depth "$DEPTH" \
    --urls "$URLS" \
    --pages "$PAGES" \
    --queries "$QUERIES" \
    --duration "$DURATION"

RESEARCH_EXIT_CODE=$?

set -e

# ==========================================================
# 7. Results
# ==========================================================

section "7. TEST RESULTS"

if [[ "$RESEARCH_EXIT_CODE" -eq 0 ]]; then
    echo "[SUCCESS] DEEPSEARCH research completed"
else
    echo "[FAILED] DEEPSEARCH exited with code: $RESEARCH_EXIT_CODE"
fi

echo
echo "Topic: $TOPIC"
echo "Model: $OLLAMA_MODEL"
echo "Exit code: $RESEARCH_EXIT_CODE"
echo "Log file: $LOG_FILE"

echo
echo "=========================================================="
echo " DEEPSEARCH TEST FINISHED"
echo "=========================================================="

exit "$RESEARCH_EXIT_CODE"