# Deploy — Operator Runbook

This directory sets up 6 independent instances of
[open-claude-in-chrome](https://github.com/noemica-io/open-claude-in-chrome)
(one per browser, ports 18765–18770).

## Supported OS / Prerequisites

- macOS 13+ or Windows 10/11
- Git — `git` must be on PATH
- Node.js 18+ — `node` and `npm` must be on PATH when running setup. The macOS
  launcher records the absolute `node` path so Chrome native messaging can start
  the host even when Chrome has a minimal PATH.
- Python 3.11+
- Windows only: PowerShell 5.1+
- One Chrome-based browser per instance. Current macOS mapping:
  - b1 → Chrome
  - b2 → Chrome Beta
  - b3 → Edge Canary
  - b4 → Edge Dev
  - b5 → Edge Beta
  - b6 → Edge

---

## macOS quickstart

From the repository root:

```bash
bash deploy/clone-oicc.sh --count 6 --dry-run
bash deploy/clone-oicc.sh --count 6
```

If Node is installed somewhere Chrome will not normally see, pass it explicitly:

```bash
NODE_BIN=/absolute/path/to/node bash deploy/clone-oicc.sh --count 6
```

When `clone-oicc.sh` patches existing OICC extension files, reload each unpacked
extension once from the browser extensions page so the background service worker
uses the updated handlers.

Load each unpacked extension from `deploy/oicc-b<N>/extension/`, then register
the native messaging host for the matching browser:

```bash
bash deploy/register-native-host-macos.sh --browser Chrome       --instance 1 --extension-id <chrome-ext-id>
bash deploy/register-native-host-macos.sh --browser Chrome-Beta  --instance 2 --extension-id <chrome-beta-ext-id>
bash deploy/register-native-host-macos.sh --browser Edge-Canary  --instance 3 --extension-id <edge-canary-ext-id>
bash deploy/register-native-host-macos.sh --browser Edge-Dev     --instance 4 --extension-id <edge-dev-ext-id>
bash deploy/register-native-host-macos.sh --browser Edge-Beta    --instance 5 --extension-id <edge-beta-ext-id>
bash deploy/register-native-host-macos.sh --browser Edge         --instance 6 --extension-id <edge-ext-id>
```

macOS writes native messaging manifests under:

| Browser | Manifest directory |
| --- | --- |
| Chrome | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts` |
| Chrome Beta | `~/Library/Application Support/Google/Chrome Beta/NativeMessagingHosts` |
| Chrome Canary | `~/Library/Application Support/Google/Chrome Canary/NativeMessagingHosts` |
| Edge | `~/Library/Application Support/Microsoft Edge/NativeMessagingHosts` |
| Edge Beta | `~/Library/Application Support/Microsoft Edge Beta/NativeMessagingHosts` |
| Edge Canary | `~/Library/Application Support/Microsoft Edge Canary/NativeMessagingHosts` |
| Edge Dev | `~/Library/Application Support/Microsoft Edge Dev/NativeMessagingHosts` |
| Brave | `~/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts` |
| Vivaldi | `~/Library/Application Support/Vivaldi/NativeMessagingHosts` |
| Opera | `~/Library/Application Support/com.operasoftware.Opera/NativeMessagingHosts` |
| Chromium | `~/Library/Application Support/Chromium/NativeMessagingHosts` |

Preview uninstall:

```bash
bash deploy/uninstall-macos.sh --dry-run
```

Run the master on macOS:

```bash
bash start.sh
tail -f logs/master.err.log
```

## OICC bridge lifecycle

The OICC MCP bridge is independent from the master. Keep the
`deploy/oicc-b<N>/host/mcp-server.js` bridge for each browser alive across
master restarts; `agent.master` does not own that process lifecycle.

Manage it explicitly with:

```bash
python -m scripts.oicc_bridge start
python -m scripts.oicc_bridge status
python -m scripts.oicc_bridge stop
```

`bash start.sh` starts the master and may clean up stale Python master/worker
processes. It also ensures the independent OICC bridge is running before the
master starts, but it must not kill or otherwise take over the existing bridge
processes. The master, workers, health checks, and browser-tab smoke checks only
connect to the configured bridge ports.

After the master is running, `start.sh` runs:

```bash
python -m scripts.browser_tab_smoke --require-listener --timeout 45
```

with no `--workers` filter, so b1-b6 must each connect to an existing bridge
listener, create, navigate, and close a temporary tab before startup is
considered verified.

---

## Windows quickstart

## Step 1 — Clone the 6 instances

Open a PowerShell window in this `deploy\` directory and run:

```powershell
powershell -File clone-oicc.ps1 -Count 6
```

This clones `open-claude-in-chrome` 6 times into `oicc-b1\` … `oicc-b6\`,
writes a `config.json` with a unique port into each clone, and creates a
`oicc-b{n}.cmd` launcher next to each directory.

To preview what would happen without doing anything:

```powershell
powershell -File clone-oicc.ps1 -Count 6 -WhatIf
```

If you need to redo a clone, use `-Force` to overwrite existing directories:

```powershell
powershell -File clone-oicc.ps1 -Count 6 -Force
```

### Verify the ports

After cloning, confirm each instance got a distinct port:

```
oicc-b1\config.json  →  { "port": 18765 }
oicc-b2\config.json  →  { "port": 18766 }
...
oicc-b6\config.json  →  { "port": 18770 }
```

### Install Node dependencies for each instance

```powershell
for ($i = 1; $i -le 6; $i++) {
    Push-Location "oicc-b$i"
    npm install
    Pop-Location
}
```

---

## Step 2 — Load the extension (unpacked) in each browser

Each browser needs to load its own copy of the extension from the matching
`oicc-b{n}\extension\` directory.

### Chrome (b1)

1. Open `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select `<install-dir>\deploy\oicc-b1\extension`
5. Note the extension ID shown under the extension name (looks like `abcdefghijklmnop`)

### Edge (b2)

1. Open `edge://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b2\extension`
4. Note the extension ID

### Brave (b3)

1. Open `brave://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b3\extension`
4. Note the extension ID

### Vivaldi (b4)

1. Open `vivaldi://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b4\extension`
4. Note the extension ID

### Opera (b5)

1. Open `opera://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b5\extension`
4. Note the extension ID

### 6th browser (b6)

1. Open its extensions page (e.g. `chrome://extensions` for Chromium)
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b6\extension`
4. Note the extension ID

---

## Step 3 — Register the native messaging host

For each (browser, instance) pair, run `register-native-host.ps1` with the
extension ID you noted in Step 2.

```powershell
powershell -File register-native-host.ps1 -Browser Chrome   -Instance 1 -ExtensionId <chrome-ext-id>
powershell -File register-native-host.ps1 -Browser Edge     -Instance 2 -ExtensionId <edge-ext-id>
powershell -File register-native-host.ps1 -Browser Brave    -Instance 3 -ExtensionId <brave-ext-id>
powershell -File register-native-host.ps1 -Browser Vivaldi  -Instance 4 -ExtensionId <vivaldi-ext-id>
powershell -File register-native-host.ps1 -Browser Opera    -Instance 5 -ExtensionId <opera-ext-id>
powershell -File register-native-host.ps1 -Browser Chromium -Instance 6 -ExtensionId <chromium-ext-id>
```

Each command:
- Writes a manifest JSON to `oicc-b{n}\manifest\com.anthropic.open_claude_in_chrome.b{n}.json`
- Creates the registry key `HKCU\Software\<browser vendor>\NativeMessagingHosts\com.anthropic.open_claude_in_chrome.b{n}` pointing to that manifest

No administrator privileges required (HKCU keys don't need elevation).

To preview without writing:

```powershell
powershell -File register-native-host.ps1 -Browser Chrome -Instance 1 -ExtensionId fakeextid123 -WhatIf
```

### Saving the extension ID for re-use

If you want to avoid retyping the ID, save it to a text file so the script
reads it automatically:

```powershell
"abcdefghijklmnop" | Set-Content oicc-b1\extension-id.txt
```

Then you can run the script without `-ExtensionId`:

```powershell
powershell -File register-native-host.ps1 -Browser Chrome -Instance 1
```

---

## Step 4 — Verify the connection

1. Restart the browser after registering the native host (Chrome-based browsers
   cache native messaging host registrations at startup).
2. Open a new tab with Claude Code running.
3. In the extension popup, check that it shows "Connected" (or equivalent status).
4. Confirm the worker can reach the MCP server:

```powershell
python -m agent.worker --worker-id b1 --skill fapiao-1688 --port 18765
```

---

## Uninstalling

To remove all registry keys and delete the cloned directories:

```powershell
powershell -File uninstall.ps1
```

Preview first:

```powershell
powershell -File uninstall.ps1 -WhatIf
```

---

## File layout after setup

```
deploy\
  clone-oicc.ps1            — Step 1 script
  clone-oicc.sh             — macOS Step 1 script
  register-native-host.ps1  — Step 3 script
  register-native-host-macos.sh
  uninstall.ps1             — Reversal script
  uninstall-macos.sh
  config.template.json      — Port field template (reference only)
  README.md                 — This file

  oicc-b1\                  — Chrome instance, port 18765
    config.json             — { "port": 18765 }
    manifest\
      com.anthropic.open_claude_in_chrome.b1.json
    host\
      native-host.js
      mcp-server.js
    ...
  oicc-b1.cmd               — .cmd launcher for native messaging
  oicc-b1.sh                — macOS launcher for native messaging

  oicc-b2\ … oicc-b6\       — Edge / Brave / Vivaldi / Opera / Chromium
  oicc-b2.cmd … oicc-b6.cmd
```

---

## Browser vendor registry paths (reference)

| Browser  | HKCU registry path prefix                        |
|----------|--------------------------------------------------|
| Chrome   | `Software\Google\Chrome`                         |
| Edge     | `Software\Microsoft\Edge`                        |
| Brave    | `Software\BraveSoftware\Brave-Browser`           |
| Vivaldi  | `Software\Vivaldi`                               |
| Opera    | `Software\Opera Software\Opera Stable`           |
| Chromium | `Software\Chromium`                              |

The full key for instance b1 on Chrome is:

```
HKCU\Software\Google\Chrome\NativeMessagingHosts\com.anthropic.open_claude_in_chrome.b1
```

Its default value must be the absolute path to the manifest JSON file.
