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
pinned_revision="$(tr -d '\r\n' < "${script_dir}/oicc-revision.txt")"
node_bin="${NODE_BIN:-$(command -v node || true)}"

if [[ -z "$node_bin" ]]; then
  echo "node not found. Install Node.js 18+ or rerun with NODE_BIN=/absolute/path/to/node." >&2
  exit 1
fi

say_dry() {
  printf '[DRY-RUN] %s\n' "$1"
}

[[ "$count" =~ ^[1-6]$ ]] || { echo "count must be 1..6" >&2; exit 2; }
patch_python="${script_dir}/../.venv/bin/python"
[[ -x "$patch_python" ]] || patch_python="${PYTHON_BIN:-python3}"

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
    [[ "$skip_npm" == "1" ]] || say_dry "run npm ci in ${instance_dir}/host"
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
    git clone --no-checkout --filter=blob:none "$repo_url" "$instance_dir"
    git -C "$instance_dir" fetch --depth 1 origin "$pinned_revision"
    git -C "$instance_dir" checkout --detach "$pinned_revision"
  fi

  [[ "$(git -C "$instance_dir" rev-parse HEAD)" == "$pinned_revision" ]] || { echo "Existing instance revision differs; back up before --force" >&2; exit 1; }

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

  "$patch_python" "${script_dir}/patch_oicc.py" "$instance_dir" --instance "$i"

  if [[ "$skip_npm" != "1" ]]; then
    host_dir="${instance_dir}/host"
    if [[ -d "${host_dir}/node_modules" ]]; then
      echo "  npm: node_modules exists, skipping"
    else
      echo "  npm ci in ${host_dir} ..."
      (cd "$host_dir" && npm ci --silent)
      echo "  npm ci done"
    fi
  fi
done

if [[ "$dry_run" != "1" ]]; then
  echo
  echo "Done. Next: load deploy/oicc-b<N>/extension/ in each browser, then run register-native-host-macos.sh."
fi
