# Deploy — Operator Runbook

This directory sets up 6 independent instances of
[open-claude-in-chrome](https://github.com/noemica-io/open-claude-in-chrome)
(one per browser, ports 18765–18770).

## Prerequisites

- Windows 10/11
- [Git for Windows](https://git-scm.com/download/win) — `git` must be on PATH
- [Node.js 18+](https://nodejs.org/) — `node` must be on PATH
- PowerShell 5.1 (built into Windows — no extra install needed)
- One Chrome-based browser per instance:
  - b1 → Chrome, b2 → Edge, b3 → Brave, b4 → Vivaldi, b5 → Opera, b6 → Chromium (or your choice)

---

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
`oicc-b{n}\` directory.

### Chrome (b1)

1. Open `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select `<install-dir>\deploy\oicc-b1` (the directory itself, not a subfolder)
5. Note the extension ID shown under the extension name (looks like `abcdefghijklmnop`)

### Edge (b2)

1. Open `edge://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b2`
4. Note the extension ID

### Brave (b3)

1. Open `brave://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b3`
4. Note the extension ID

### Vivaldi (b4)

1. Open `vivaldi://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b4`
4. Note the extension ID

### Opera (b5)

1. Open `opera://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b5`
4. Note the extension ID

### 6th browser (b6)

1. Open its extensions page (e.g. `chrome://extensions` for Chromium)
2. Enable **Developer mode**
3. Click **Load unpacked** → select `oicc-b6`
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
  register-native-host.ps1  — Step 3 script
  uninstall.ps1             — Reversal script
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
