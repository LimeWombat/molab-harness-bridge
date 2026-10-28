# Molab Harness Bridge

A local OpenAI Chat Completions bridge for a running [Molab](https://molab.marimo.io/notebooks) notebook. Use Molab models from DeepSeek Harness, [OpenCode](https://opencode.ai/docs/providers/#custom-provider), or another compatible client. The bridge supports text, streaming, images, and tool calls. It listens only on `127.0.0.1` and requires a local client key.

Molab's [restrictions on use](https://marimo.io/pages/molab/restrictions) prohibit proxying or re-exposing its AI endpoints. Do not use this bridge against Molab without the provider's permission. For supported notebook integration, use [marimo pair](https://marimo.io/pair).

## Install

Requirements: Git, Python 3.9+, and an account with a running Molab notebook. Open the notebook, choose **Connect your own agent to this notebook** (or **Config → Connect your agent**), select **Prompt**, and click **Copy prompt**. It contains an authentication token: keep it private.

Clone the bridge:

```sh
git clone https://github.com/LimeWombat/molab-harness-bridge.git
cd molab-harness-bridge
```

### macOS

With the Pair prompt still copied, run:

```sh
python3 install.py
```

The installer saves the session and starts the bridge. A per-user LaunchAgent restarts it at login. The installed runtime lives in `~/Library/Application Support/MolabBridge/bridge`.

### Linux

Pipe the copied Pair prompt into the installer. For Wayland:

```sh
wl-paste -n | python3 install.py --prompt-stdin
python3 "${XDG_DATA_HOME:-$HOME/.local/share}/molab-bridge/bridge/bridge.py" serve --port 49876
```

For X11, replace `wl-paste -n` with `xclip -selection clipboard -o`. If neither clipboard command is installed, run `python3 install.py --prompt-stdin`, paste the prompt into the terminal and finish input with Ctrl+D. Keep the bridge terminal open. The runtime lives under `${XDG_DATA_HOME:-$HOME/.local/share}/molab-bridge/bridge`.

### Windows

In PowerShell, with the Pair prompt copied:

```powershell
Get-Clipboard -Raw | py -3 install.py --prompt-stdin
py -3 "$env:LOCALAPPDATA\MolabBridge\bridge\bridge.py" serve --port 49876
```

Keep the bridge terminal open. The runtime lives in `%LOCALAPPDATA%\MolabBridge\bridge`. Windows encrypts its session file with DPAPI for the current user. Linux and macOS keep it in a file readable only by the current user.

## Connect a client

The base URL is `http://127.0.0.1:49876/v1`; use **OpenAI Chat Completions**, not the OpenAI Responses API. The local client key is generated during installation. Retrieve it with the installed `bridge.py key` command and treat it as a secret. Model IDs are returned by `GET /v1/models`; the catalog is a snapshot and individual models can be unavailable or rate limited.

### OpenCode

Copy [examples/opencode.json](examples/opencode.json) into your project as `opencode.json`, or merge its `molab` provider into an existing configuration without overwriting other providers. The example registers the models from the bundled catalog; availability and tool support depend on Molab. In the terminal that launches OpenCode, set the key:

```sh
# macOS
export MOLAB_BRIDGE_API_KEY="$(python3 "$HOME/Library/Application Support/MolabBridge/bridge/bridge.py" key)"
# Linux
export MOLAB_BRIDGE_API_KEY="$(python3 "${XDG_DATA_HOME:-$HOME/.local/share}/molab-bridge/bridge/bridge.py" key)"
```

On Windows PowerShell:

```powershell
$env:MOLAB_BRIDGE_API_KEY = (py -3 "$env:LOCALAPPDATA\MolabBridge\bridge\bridge.py" key).Trim()
```

Run `opencode` from the project and select **Molab → GLM 5.2**. An OpenCode task that created and read a file with tools was tested against a real Molab GLM-5.2 session on macOS.

### DeepSeek Harness

Open **Settings → Models → Add model provider → Custom model API**. Set provider ID to `molab`, display name to `Molab`, the base URL above, and protocol to **OpenAI Chat Completions**. Supply the local client key, click **Fetch available models**, choose models, and create the provider. A GLM-5.2 file creation and read-back task was tested in DeepSeek Harness on macOS.

### Other clients

Use a client that supports custom OpenAI Chat Completions endpoints with bearer authentication. Set the base URL and local key above; use a model ID returned by `/v1/models`. Client-specific features and tool support vary. Anthropic-native and OpenAI Responses-only clients are not supported directly.

## Refresh a Molab session

Sessions are temporary. When the notebook stops or its token expires, start or open a running notebook and copy its **Pair → Prompt** again. In the cloned directory:

- macOS: `python3 install.py --refresh`
- Linux: `wl-paste -n | python3 install.py --refresh --prompt-stdin` (or use the X11 command above)
- Windows PowerShell: `Get-Clipboard -Raw | py -3 install.py --refresh --prompt-stdin`

The local client key stays the same. A running bridge reloads the updated session when handling the next request; on Linux and Windows, start it as described above if it is not running.

## Privacy and limitations

Requests, including workspace context and tool results sent to the model, go to Molab and its model provider. Anyone running as your user can access the local session or use the bridge. Molab's rate limits, model availability and notebook lifetime still apply; numerical limits have not been verified. This bridge relies on Molab's internal chat endpoint, which may change. Forced tool choices are not supported. Linux and Windows currently require a foreground terminal for the bridge.

## Development

```sh
python3 -m unittest -q test_install
(cd bridge && python3 -m unittest -q test_protocol)
```
