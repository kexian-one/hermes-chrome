from __future__ import annotations

import argparse
import json
from pathlib import Path


def replace_once(path: Path, anchor: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8-sig")
    if replacement in text:
        return
    if text.count(anchor) != 1:
        raise ValueError(f"Patch anchor changed: {path.name}")
    path.write_text(text.replace(anchor, replacement), encoding="utf-8")


def patch(root: Path, instance: int) -> None:
    # This revision uses a shared TCP runtime; newer pipe-only revisions need
    # a coordinated update of the Python bridge, not a silent partial patch.
    for name in ("tool-runtime.js", "native-host.js"):
        replace_once(root / "host" / name, "function getPort() {\n", '''function getPort() {
  if (process.env.OICC_PORT) {
    const port = Number(process.env.OICC_PORT);
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid OICC_PORT");
    return port;
  }
''')
    definitions = (root / "host" / "tool-definitions.js").read_text(encoding="utf-8-sig")
    background = root / "extension" / "background.js"
    for name in ("tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate", "javascript_tool"):
        if f'name: "{name}"' not in definitions or f"async {name}(" not in background.read_text(encoding="utf-8-sig"):
            raise ValueError(f"Required browser tool missing: {name}")
    replace_once(background, 'const NATIVE_HOST_NAME = "com.anthropic.open_claude_in_chrome";', f'const NATIVE_HOST_NAME = "com.anthropic.open_claude_in_chrome.b{instance}";')
    manifest_path = root / "extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest["name"] = f"AI Chrome Assistant (b{instance})"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--instance", type=int, choices=range(1, 7), required=True)
    args = parser.parse_args()
    patch(args.root, args.instance)
