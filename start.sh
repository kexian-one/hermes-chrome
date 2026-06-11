#!/usr/bin/env bash
set -euo pipefail

# macOS/Linux launcher for the master orchestrator.
# Run from anywhere with: bash start.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

check_only=0
case "${1:-}" in
  --check)
    check_only=1
    shift
    ;;
  -h|--help)
    cat <<'EOF'
Usage: bash start.sh [--check]

Start the master orchestrator in the background.

Options:
  --check   Validate Python/dependencies and exit without killing or starting processes.
EOF
    exit 0
    ;;
esac

choose_python() {
  if [[ -n "${ALL_IN_AI_PYTHON:-}" ]]; then
    PY_CMD=("$ALL_IN_AI_PYTHON")
    return
  fi

  local candidate
  for candidate in \
    "${ROOT}/.venv/bin/python" \
    python3.13 \
    python3.12 \
    python3.11 \
    python3 \
    python
  do
    if [[ "$candidate" == */* ]]; then
      [[ -x "$candidate" ]] || continue
    else
      command -v "$candidate" >/dev/null 2>&1 || continue
    fi
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      PY_CMD=("$candidate")
      return
    fi
  done

  if command -v uv >/dev/null 2>&1; then
    PY_CMD=(uv run python)
    return
  fi

  echo "[start.sh] Python 3.11+ not found. Install Python 3.11+ or uv, or set ALL_IN_AI_PYTHON." >&2
  exit 1
}

choose_python

"${PY_CMD[@]}" - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit(f"Python 3.11+ required, got {sys.version.split()[0]}")
try:
    import yaml  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(
        "PyYAML is missing for the selected Python. Run `uv sync`, "
        "`pip install -e .`, or set ALL_IN_AI_PYTHON to a prepared venv."
    ) from exc
PY

py_desc="$("${PY_CMD[@]}" -c 'import sys; print(sys.executable)' 2>/dev/null || printf '%s' "${PY_CMD[*]}")"
echo "[start.sh] using Python: ${py_desc}"

if [[ "$check_only" == "1" ]]; then
  echo "[start.sh] check passed"
  exit 0
fi

echo "[start.sh] killing old project processes..."

python_re='python(3)? .*agent\.(master|worker|bot)'
node_re='node .*deploy/oicc-b[0-9]+/host/mcp-server\.js'
killed=0

while IFS= read -r line; do
  pid="${line%% *}"
  cmd="${line#* }"
  [[ "$pid" == "$$" ]] && continue
  if [[ "$cmd" =~ $python_re || "$cmd" =~ $node_re ]]; then
    kill -9 "$pid" 2>/dev/null || true
    echo "  killed pid=${pid}"
    killed=$((killed + 1))
  fi
done < <(ps -axo pid=,command= | sed 's/^ *//')

if [[ "$killed" == "0" ]]; then
  echo "  (no existing master/worker/mcp processes)"
fi

sleep 0.8

echo "[start.sh] checking macOS native-messaging manifests..."
"${PY_CMD[@]}" - "$ROOT" <<'PY'
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
config_path = root / "config.yaml"
if not config_path.is_file():
    print("  config.yaml missing, skipping native host self-heal")
    raise SystemExit(0)

cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
browsers = cfg.get("browsers") if isinstance(cfg, dict) else None
if not isinstance(browsers, dict):
    print("  no browsers block, skipping native host self-heal")
    raise SystemExit(0)

home = Path.home()
targets = {
    "chrome": home / "Library/Application Support/Google/Chrome/NativeMessagingHosts",
    "edge": home / "Library/Application Support/Microsoft Edge/NativeMessagingHosts",
    "brave": home / "Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts",
    "vivaldi": home / "Library/Application Support/Vivaldi/NativeMessagingHosts",
    "opera": home / "Library/Application Support/com.operasoftware.Opera/NativeMessagingHosts",
    "chromium": home / "Library/Application Support/Chromium/NativeMessagingHosts",
}

for worker, data in browsers.items():
    if not isinstance(data, dict):
        continue
    name = str(data.get("name", "")).strip().lower()
    browser_dir = targets.get(name)
    if browser_dir is None:
        print(f"  {worker}: unknown browser {name!r}, skipping")
        continue
    host = f"com.anthropic.open_claude_in_chrome.{worker}"
    source = root / "deploy" / f"oicc-{worker}" / "manifest" / f"{host}.json"
    target = browser_dir / f"{host}.json"
    if not source.is_file():
        print(f"  {worker}: manifest missing at {source}, skipping")
        continue
    browser_dir.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8"):
        print(f"  {worker} ({name}): OK")
        continue
    shutil.copy2(source, target)
    print(f"  {worker} ({name}): repaired -> {target}")
PY

mkdir -p logs
log_err="${ROOT}/logs/master.err.log"
log_out="${ROOT}/logs/master.out.log"
ts="$(date '+%Y-%m-%d %H:%M:%S')"
printf '\n========== restart %s ==========\n' "$ts" >> "$log_err"
printf '\n========== restart %s ==========\n' "$ts" >> "$log_out"

echo "[start.sh] launching master..."
nohup "${PY_CMD[@]}" -u -m agent.master >> "$log_out" 2>> "$log_err" &
pid=$!
sleep 0.5

if ! kill -0 "$pid" 2>/dev/null; then
  echo "[start.sh] master exited immediately. Check ${log_err}" >&2
  exit 1
fi

echo "[start.sh] master started, pid=${pid}"
echo "[start.sh] main log: ${log_err}"
echo "[start.sh] tail with: tail -f ${log_err}"
