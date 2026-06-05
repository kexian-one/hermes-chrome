#!/usr/bin/env bash
set -euo pipefail

count=6
dry_run=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --count)
      count="$2"
      shift 2
      ;;
    --dry-run|--what-if)
      dry_run=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: bash deploy/uninstall-macos.sh [--count 6] [--dry-run]

Remove macOS native messaging manifests and deploy/oicc-bN clone directories.
EOF
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

browser_dirs=(
  "${HOME}/Library/Application Support/Google/Chrome/NativeMessagingHosts"
  "${HOME}/Library/Application Support/Microsoft Edge/NativeMessagingHosts"
  "${HOME}/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts"
  "${HOME}/Library/Application Support/Vivaldi/NativeMessagingHosts"
  "${HOME}/Library/Application Support/com.operasoftware.Opera/NativeMessagingHosts"
  "${HOME}/Library/Application Support/Chromium/NativeMessagingHosts"
)

act() {
  if [[ "$dry_run" == "1" ]]; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}

echo "=== Uninstall: macOS native messaging manifests ==="
for ((i = 1; i <= count; i++)); do
  host_name="com.anthropic.open_claude_in_chrome.b${i}"
  for dir in "${browser_dirs[@]}"; do
    manifest="${dir}/${host_name}.json"
    if [[ "$dry_run" == "1" ]]; then
      echo "[DRY-RUN] rm -f ${manifest}"
    elif [[ -f "$manifest" ]]; then
      rm -f "$manifest"
      echo "Removed: ${manifest}"
    fi
  done
done

echo
echo "=== Uninstall: clone directories and launchers ==="
for ((i = 1; i <= count; i++)); do
  instance_dir="${script_dir}/oicc-b${i}"
  launcher="${script_dir}/oicc-b${i}.sh"
  if [[ "$dry_run" == "1" ]]; then
    echo "[DRY-RUN] rm -rf ${instance_dir}"
    echo "[DRY-RUN] rm -f ${launcher}"
    continue
  fi
  case "$instance_dir" in
    "$script_dir"/oicc-b*) [[ -d "$instance_dir" ]] && rm -rf "$instance_dir" ;;
    *) echo "unsafe instance path: $instance_dir" >&2; exit 1 ;;
  esac
  [[ -f "$launcher" ]] && rm -f "$launcher"
done

[[ "$dry_run" == "1" ]] || echo "Uninstall complete."
