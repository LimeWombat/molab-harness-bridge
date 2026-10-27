# Molab Harness Bridge

A local OpenAI Chat Completions bridge for a running [Molab](https://molab.marimo.io/notebooks) notebook. It lets DeepSeek Harness use available Molab models, including tool calls. The bridge binds to `127.0.0.1` and requires a local client key.

The model prompts come from [togg53192-cmd/jailbreaks](https://github.com/togg53192-cmd/jailbreaks), included as a Git submodule pinned to a specific revision. The upstream repository did not contain a license when checked; consult its owner before redistributing its contents. Prompts are third-party instructions and may weaken safeguards or interfere with tool use.

## Quick installation on macOS

Requirements: Git, Python 3.9+, DeepSeek Harness, and an account able to use a Molab notebook.

1. Open a running Molab notebook. Choose **Connect your own agent to this notebook** (or **Config → Connect your agent**), select **Prompt**, then click **Copy prompt**. This contains a secret; do not paste it into a public chat.
2. In Terminal run:

   ```sh
   git clone --recurse-submodules https://github.com/LimeWombat/molab-harness-bridge.git
   cd molab-harness-bridge
   python3 install.py
   ```

   If you cloned without submodules, run `git submodule update --init` first. The installer reads the Pair prompt from the clipboard, stores the connection privately, and starts the service at `http://127.0.0.1:49876/v1`. An existing session and local client key are preserved. It installs a per-user LaunchAgent that restarts at login.
3. In DeepSeek Harness open **Settings → Models → Add model provider → Custom model API**. Set provider ID to `molab`, display name to `Molab`, base URL to `http://127.0.0.1:49876/v1`, and protocol to **OpenAI Chat Completions**. Copy the local key into your clipboard using the command below, paste it into the API key field, click **Fetch available models**, select the models, and create the provider.

   ```sh
   python3 -c 'import json,pathlib; print(json.loads((pathlib.Path.home()/"Library/Application Support/MolabBridge/bridge/session.json").read_text())["local_key"], end="")' | pbcopy
   ```

4. Open a workspace, choose a model under **Molab**, and try a task. GLM-5.2 was tested with a file-creation and read-back task using DeepSeek Harness tools.

## Refresh a Molab session

Molab sessions are temporary. If the notebook stops or the token expires, open a running notebook, copy its **Pair → Prompt** again, then run from the cloned directory:

```sh
python3 install.py --refresh
```

The local client key remains the same, so DeepSeek Harness does not need to be reconfigured. The notebook must remain available. A rate limit or a model returning 404 comes from the upstream service; availability can change. The model catalog is a snapshot, not a guarantee that every listed model is currently available. The implementation relies on Molab's internal chat endpoint, which may change.

## Privacy and limitations

Requests, including workspace context and tool results supplied to the model, are sent to Molab and its model provider. The bridge keeps tokens in a private local file and does not expose a network listener beyond loopback. Anyone able to read the local session file can use the bridge or the notebook token. Streaming, text, images and tool calls are translated; forced tool choices are not supported. The numerical Molab limits were not verified.

## Development

```sh
python3 -m unittest -q test_install
(cd bridge && python3 -m unittest -q test_protocol)
```

Automatic installation currently targets macOS. Other systems can run `bridge/bridge.py` manually after preparing a private session file; service setup is not included.
