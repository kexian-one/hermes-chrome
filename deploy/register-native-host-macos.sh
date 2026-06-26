#!/usr/bin/env bash
set -euo pipefail

browser=""
instance=""
extension_id=""
dry_run=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --browser)
      browser="$2"
      shift 2
      ;;
    --instance)
      instance="$2"
      shift 2
      ;;
    --extension-id)
      extension_id="$2"
      shift 2
      ;;
    --dry-run|--what-if)
      dry_run=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: bash deploy/register-native-host-macos.sh --browser Chrome --instance 1 [--extension-id <id>] [--dry-run]

Register one open-claude-in-chrome native messaging host on macOS. If
--extension-id is omitted, the script reads deploy/oicc-bN/extension-id.txt.
EOF
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$browser" || -z "$instance" ]]; then
  echo "--browser and --instance are required" >&2
  exit 2
fi
if ! [[ "$instance" =~ ^[1-6]$ ]]; then
  echo "--instance must be 1..6" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
host_name="com.anthropic.open_claude_in_chrome.b${instance}"
instance_dir="${script_dir}/oicc-b${instance}"
manifest_dir="${instance_dir}/manifest"
local_manifest="${manifest_dir}/${host_name}.json"
launcher="${script_dir}/oicc-b${instance}.sh"

browser_key="$(printf '%s' "$browser" | tr '[:upper:]' '[:lower:]')"
case "$browser_key" in
  chrome) browser_dir="${HOME}/Library/Application Support/Google/Chrome/NativeMessagingHosts" ;;
  chrome-beta|chromebeta) browser_dir="${HOME}/Library/Application Support/Google/Chrome Beta/NativeMessagingHosts" ;;
  chrome-canary|chromecanary) browser_dir="${HOME}/Library/Application Support/Google/Chrome Canary/NativeMessagingHosts" ;;
  edge) browser_dir="${HOME}/Library/Application Support/Microsoft Edge/NativeMessagingHosts" ;;
  edge-beta|edgebeta) browser_dir="${HOME}/Library/Application Support/Microsoft Edge Beta/NativeMessagingHosts" ;;
  edge-canary|edgecanary) browser_dir="${HOME}/Library/Application Support/Microsoft Edge Canary/NativeMessagingHosts" ;;
  edge-dev|edgedev) browser_dir="${HOME}/Library/Application Support/Microsoft Edge Dev/NativeMessagingHosts" ;;
  brave) browser_dir="${HOME}/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts" ;;
  vivaldi) browser_dir="${HOME}/Library/Application Support/Vivaldi/NativeMessagingHosts" ;;
  opera) browser_dir="${HOME}/Library/Application Support/com.operasoftware.Opera/NativeMessagingHosts" ;;
  chromium) browser_dir="${HOME}/Library/Application Support/Chromium/NativeMessagingHosts" ;;
  *)
    echo "unsupported browser: $browser (Chrome/Chrome-Beta/Chrome-Canary/Edge/Edge-Beta/Edge-Canary/Edge-Dev/Brave/Vivaldi/Opera/Chromium)" >&2
    exit 2
    ;;
esac

browser_manifest="${browser_dir}/${host_name}.json"

if [[ -z "$extension_id" ]]; then
  id_file="${instance_dir}/extension-id.txt"
  if [[ -f "$id_file" ]]; then
    extension_id="$(tr -d '[:space:]' < "$id_file")"
    echo "Read extension id from ${id_file}: ${extension_id}"
  else
    echo "extension id missing. Pass --extension-id or save it to ${id_file}" >&2
    exit 2
  fi
fi

allowed_origin="chrome-extension://${extension_id}/"

if [[ "$dry_run" == "1" ]]; then
  echo "[DRY-RUN] local manifest:   ${local_manifest}"
  echo "[DRY-RUN] browser manifest: ${browser_manifest}"
  echo "[DRY-RUN] launcher path:    ${launcher}"
  echo "[DRY-RUN] allowed origin:   ${allowed_origin}"
  exit 0
fi

if [[ ! -x "$launcher" ]]; then
  echo "launcher not executable: ${launcher}. Run bash deploy/clone-oicc.sh first." >&2
  exit 1
fi

mkdir -p "$manifest_dir" "$browser_dir"

python3 - "$local_manifest" "$host_name" "$launcher" "$allowed_origin" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = {
    "name": sys.argv[2],
    "description": f"Open Claude in Chrome ({sys.argv[2]})",
    "path": sys.argv[3],
    "type": "stdio",
    "allowed_origins": [sys.argv[4]],
}
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

cp "$local_manifest" "$browser_manifest"

echo "Wrote local manifest:   ${local_manifest}"
echo "Wrote browser manifest: ${browser_manifest}"
echo "Registered ${host_name} for ${browser}"
