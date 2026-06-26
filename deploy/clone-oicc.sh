#!/usr/bin/env bash
set -euo pipefail

count=6
force=0
skip_npm=0
dry_run=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)
      count="$2"
      shift 2
      ;;
    --force)
      force=1
      shift
      ;;
    --skip-npm-install)
      skip_npm=1
      shift
      ;;
    --dry-run|--what-if)
      dry_run=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: bash deploy/clone-oicc.sh [--count 6] [--force] [--skip-npm-install] [--dry-run]

Clone open-claude-in-chrome into deploy/oicc-b1..bN, patch each instance for
its own native host name and OICC_PORT, and generate macOS-compatible launchers.
EOF
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

repo_url="https://github.com/noemica-io/open-claude-in-chrome.git"
base_port=18765
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
node_bin="${NODE_BIN:-$(command -v node || true)}"

if [[ -z "$node_bin" ]]; then
  echo "node not found. Install Node.js 18+ or rerun with NODE_BIN=/absolute/path/to/node." >&2
  exit 1
fi

say_dry() {
  printf '[DRY-RUN] %s\n' "$1"
}

patch_file() {
  local file="$1"
  local pattern="$2"
  local replacement="$3"
  local description="$4"

  if [[ ! -f "$file" ]]; then
    printf '    skip %s: file missing %s\n' "$description" "$file"
    return
  fi

  python3 - "$file" "$pattern" "$replacement" "$description" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
pattern = sys.argv[2]
replacement = sys.argv[3]
description = sys.argv[4]

text = path.read_text(encoding="utf-8")
new, count = re.subn(pattern, replacement, text, count=1)
if count == 0:
    if replacement in text:
        print(f"    {description}: already applied")
    else:
        print(f"    {description}: pattern not found")
    raise SystemExit(0)
path.write_text(new, encoding="utf-8")
print(f"    {description}: patched")
PY
}

patch_file_if_missing() {
  local file="$1"
  local marker="$2"
  local pattern="$3"
  local replacement="$4"
  local description="$5"

  if [[ -f "$file" ]] && grep -Fq "$marker" "$file"; then
    printf '    %s: already applied\n' "$description"
    return
  fi
  patch_file "$file" "$pattern" "$replacement" "$description"
}

for ((i = 1; i <= count; i++)); do
  instance_name="oicc-b${i}"
  instance_dir="${script_dir}/${instance_name}"
  port=$((base_port + i - 1))
  launcher="${script_dir}/${instance_name}.sh"

  if [[ "$dry_run" == "1" ]]; then
    say_dry "clone ${repo_url} -> ${instance_dir} if needed"
    say_dry "write ${instance_dir}/config.json port=${port}"
    say_dry "write launcher ${launcher} node=${node_bin}"
    say_dry "patch extension and host files for ${instance_name}"
    [[ "$skip_npm" == "1" ]] || say_dry "run npm install in ${instance_dir}/host"
    continue
  fi

  printf '=== %s (port %s) ===\n' "$instance_name" "$port"

  if [[ -d "$instance_dir" ]]; then
    if [[ "$force" == "1" ]]; then
      case "$instance_dir" in
        "$script_dir"/oicc-b*) rm -rf "$instance_dir" ;;
        *) echo "unsafe instance path: $instance_dir" >&2; exit 1 ;;
      esac
    else
      echo "  clone: exists, skip clone (patches still re-applied)"
    fi
  fi

  if [[ ! -d "$instance_dir" ]]; then
    git clone --depth 1 "$repo_url" "$instance_dir"
  fi

  cat > "${instance_dir}/config.json" <<EOF
{
  "port": ${port}
}
EOF
  printf '  config.json port=%s written\n' "$port"

  cat > "$launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
cd "\$SCRIPT_DIR/${instance_name}"
export OICC_PORT=${port}
exec "${node_bin}" host/native-host.js "\$@"
EOF
  chmod +x "$launcher"
  printf '  launcher %s written\n' "$launcher"

  echo "  patches:"
  patch_file \
    "${instance_dir}/extension/background.js" \
    'const NATIVE_HOST_NAME = "com\.anthropic\.open_claude_in_chrome";?' \
    "const NATIVE_HOST_NAME = \"com.anthropic.open_claude_in_chrome.b${i}\";" \
    "background.js NATIVE_HOST_NAME -> .b${i}"

  patch_file \
    "${instance_dir}/extension/manifest.json" \
    '"name": "Open Claude in Chrome"' \
    "\"name\": \"AI Chrome Assistant (b${i})\"" \
    "manifest.json name -> AI Chrome Assistant (b${i})"

  env_hook_mcp=$'function getPort() {\n  if (process.env.OICC_PORT) {\n    const p = parseInt(process.env.OICC_PORT, 10);\n    if (!isNaN(p) && p > 0) return p;\n  }\n  const configPath ='
  patch_file \
    "${instance_dir}/host/mcp-server.js" \
    'function getPort\(\) \{\r?\n  const configPath =' \
    "$env_hook_mcp" \
    "mcp-server.js getPort honors OICC_PORT"

  host_close_tool=$'// 2b. tabs_close_mcp\nserver.tool(\n  "tabs_close_mcp",\n  "Close a tab in the MCP tab group after a temporary browser operation is complete.",\n  {\n    tabId: z.number().describe("Tab ID to close. Must be a tab in the current group."),\n  },\n  async (args) => callTool("tabs_close_mcp", args)\n);\n\n// 3. navigate'
  patch_file_if_missing \
    "${instance_dir}/host/mcp-server.js" \
    '"tabs_close_mcp"' \
    '// 3[.] navigate' \
    "$host_close_tool" \
    "mcp-server.js adds tabs_close_mcp"

  signal_guard=$'if (process.env.OICC_KEEPALIVE_IGNORE_SIGHUP === "1") {\n  process.on("SIGTERM", () => {});\n  process.on("SIGHUP", () => {});\n} else {\n  process.on("SIGTERM", shutdown);\n  process.on("SIGHUP", shutdown);\n}\nprocess.on("SIGINT", shutdown);\n'
  patch_file_if_missing \
    "${instance_dir}/host/mcp-server.js" \
    'process.on("SIGTERM", () => {})' \
    'process[.]on[(]"SIGTERM", shutdown[)];\r?\nprocess[.]on[(]"SIGINT", shutdown[)];\r?\n(?:if [(]process[.]env[.]OICC_KEEPALIVE_IGNORE_SIGHUP === "1"[)] [{]\r?\n  process[.]on[(]"SIGHUP", [(][)] => [{][}][)];\r?\n[}] else [{]\r?\n  process[.]on[(]"SIGHUP", shutdown[)];\r?\n[}]\r?\n)?|if [(]process[.]env[.]OICC_KEEPALIVE_IGNORE_SIGHUP !== "1"[)] [{]\r?\n  process[.]on[(]"SIGHUP", shutdown[)];\r?\n[}]|process[.]on[(]"SIGHUP", shutdown[)];' \
    "$signal_guard" \
    "mcp-server.js can ignore SIGHUP/SIGTERM for master keepalive"

  env_hook_native=$'function getPort() {\n  if (process.env.OICC_PORT) {\n    const p = parseInt(process.env.OICC_PORT, 10);\n    if (!isNaN(p) && p > 0) return p;\n  }\n  const configPath = path.join('
  patch_file \
    "${instance_dir}/host/native-host.js" \
    'function getPort\(\) \{\r?\n  const configPath = path\.join\(' \
    "$env_hook_native" \
    "native-host.js getPort honors OICC_PORT"

  extension_close_handler=$'  async tabs_close_mcp(args) {\n    const { tabId } = args;\n    if (!(await isInGroup(tabId))) return { content: [{ type: "text", text: `Tab ${tabId} is not in the MCP group.` }] };\n    try {\n      await chrome.tabs.remove(tabId);\n      tabGroupTabs.delete(tabId);\n      return { content: [{ type: "text", text: `Closed tab ${tabId}.` }] };\n    } catch (error) {\n      return { content: [{ type: "text", text: `Error closing tab ${tabId}: ${error.message}` }] };\n    }\n  },\n\n  async navigate(args) {'
  patch_file_if_missing \
    "${instance_dir}/extension/background.js" \
    'async tabs_close_mcp(args)' \
    '  async navigate[(]args[)] [{]' \
    "$extension_close_handler" \
    "background.js adds tabs_close_mcp"

  if [[ "$skip_npm" != "1" ]]; then
    host_dir="${instance_dir}/host"
    if [[ -d "${host_dir}/node_modules" ]]; then
      echo "  npm: node_modules exists, skipping"
    else
      echo "  npm install in ${host_dir} ..."
      (cd "$host_dir" && npm install --silent)
      echo "  npm install done"
    fi
  fi
done

if [[ "$dry_run" != "1" ]]; then
  echo
  echo "Done. Next: load deploy/oicc-b<N>/extension/ in each browser, then run register-native-host-macos.sh."
fi
