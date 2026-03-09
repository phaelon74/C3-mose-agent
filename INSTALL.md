# Installation Guide

Complete setup instructions for deploying the Mose Agent on a Linux homelab server.

## Prerequisites

| Requirement | Minimum |
|---|---|
| OS | Ubuntu 22.04+ (or any systemd-based Linux) |
| Python | 3.11+ |
| NVIDIA GPUs | 4x RTX 3060 (12GB each, 48GB total) |
| NVIDIA Driver | 570+ |
| CUDA Toolkit | 13.0 |
| RAM | 32GB+ recommended |
| Disk | 30GB free (model weights + venv + runtime data) |

Verify your GPU stack before proceeding:

```bash
nvidia-smi          # driver loaded, GPUs visible
nvcc --version      # should report CUDA 13.0
```

---

## 1. Create the Service User

Create a dedicated `Mose` user to run the agent. No sudo rights are needed for the user itself.

```bash
sudo useradd -m -s /bin/bash Mose
sudo passwd Mose
```

Add the user to groups required for GPU access:

```bash
sudo usermod -aG video Mose
sudo usermod -aG render Mose
```

---

## 2. Clone the Repository

```bash
sudo -u Mose -i   # switch to the Mose user
cd ~
git clone https://github.com/phaelon74/C3-luna-agent.git mose-agent
cd mose-agent
```

---

## 3. Create the Python Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

To install dev/test dependencies as well:

```bash
pip install -e ".[dev]"
```

### vLLM (LLM Server)

vLLM must be installed in the same venv. For CUDA 13.0, install PyTorch with
CUDA 13 first, then vLLM. See the
[vLLM install docs](https://docs.vllm.ai/en/latest/getting_started/installation.html)
for your CUDA version.

```bash
# For CUDA 13.0, install PyTorch cu130 first:
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130

# Then install vLLM
pip install vllm
```

The model weights (`QuantTrio/Qwen3.5-27B-AWQ`) are downloaded automatically on
first launch by vLLM/Hugging Face.

---

## 4. Create Runtime Directories

The `data/` directory is gitignored and must be created manually:

```bash
mkdir -p data/{logs,workspace,tool_outputs}
```

Final layout at runtime:

```
~/mose-agent/
├── data/
│   ├── memory.db            # SQLite database (created on first run)
│   ├── logs/                # Structured JSON logs
│   │   └── mose-YYYY-MM-DD.jsonl
│   ├── workspace/           # Sandboxed working directory for tools
│   └── tool_outputs/        # Persisted large tool output files
```

---

## 5. Configure the Environment File

Copy the example and fill in the values you need:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set at minimum the interface you want to use:

```bash
# For Discord bot mode (omit for CLI-only)
DISCORD_TOKEN=your-discord-bot-token

# For Signal bot mode (requires signal-cli, see Section 9)
SIGNAL_PHONE=+1234567890
```

The `.env` file can also hold API keys for any MCP-connected services
(Radarr, Sonarr, Plex, Proxmox, etc.) — see `.env.example` for the full list.

---

## 6. Configure the Agent

Edit `config.toml` to match your hardware. The defaults work for the reference
hardware (4x RTX 3060, vLLM on port 8001):

```toml
[llm]
endpoint = "http://localhost:8001/v1"
model = "QuantTrio/Qwen3.5-27B-AWQ"
max_tokens = 16384
context_window = 98304

[memory]
db_path = "data/memory.db"
summary_interval = 8        # summarize every N messages

[agent]
workspace = "data/workspace"
skills_path = "skills"

[observe]
log_dir = "data/logs"
log_level = "INFO"
```

Environment variables override config file values — see the table in the README
for the full mapping.

---

## 7. Configure MCP Servers (Optional)

MCP servers give the agent additional tools beyond the built-ins. Copy the
example and edit:

```bash
cp mcp_servers.example.json mcp_servers.json
```

Each server entry specifies a command to spawn via stdio transport:

```json
{
  "servers": {
    "paper_db": {
      "command": ".venv/bin/python",
      "args": ["data/workspace/paper_db/paper_db_server.py"],
      "transport": "stdio"
    }
  }
}
```

If you don't need MCP tools, create an empty config:

```json
{ "servers": {} }
```

---

## 8. Install Systemd Services

These commands require an admin user with sudo. The services themselves run as
the `Mose` user — no elevated privileges at runtime.

```bash
# Copy service files
sudo cp /home/Mose/mose-agent/worker-agent.service /etc/systemd/system/
sudo cp /home/Mose/mose-agent/mose-agent.service /etc/systemd/system/

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable worker-agent
sudo systemctl enable mose-agent
```

### Start the LLM server first

The agent depends on the LLM server (`worker-agent`), so start it first and
wait for it to finish loading the model:

```bash
sudo systemctl start worker-agent
journalctl -u worker-agent -f
```

Wait until you see vLLM listening on port 8001, then start the agent:

```bash
sudo systemctl start mose-agent
journalctl -u mose-agent -f
```

### Service dependency chain

```
worker-agent.service       (vLLM, port 8001)
       ↑
mose-agent.service         (the bot — Requires=worker-agent)
       ↑
signal-cli-daemon.service  (optional — Wants=signal-cli-daemon)
```

---

## 9. Signal Bot Setup (Optional)

If you want to use Signal instead of (or alongside) Discord:

### Install signal-cli

```bash
# Download the latest release (0.14.0+ required for JSON-RPC daemon mode)
# See: https://github.com/AsamK/signal-cli/releases
sudo mkdir -p /opt/signal-cli
sudo tar xf signal-cli-0.14.x-Linux.tar.gz -C /opt/signal-cli --strip-components=1
```

### Link your Signal account

```bash
sudo -u Mose /opt/signal-cli/bin/signal-cli link -n "Mose Agent"
```

Scan the QR code with your phone's Signal app (Settings > Linked Devices).

### Install the daemon service

Edit `signal-cli-daemon.service` and replace `+YOUR_PHONE_NUMBER` with your
actual linked Signal number, then install:

```bash
sudo cp /home/Mose/mose-agent/signal-cli-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signal-cli-daemon
```

Set the phone number in `.env`:

```bash
SIGNAL_PHONE=+1234567890
```

---

## 10. Verify the Installation

### Quick smoke test (CLI mode)

Run without Discord/Signal tokens to get an interactive REPL:

```bash
sudo -u Mose -i
cd ~/mose-agent
source .venv/bin/activate
python -m mose
```

You should see:

```
Mose CLI (type 'exit' or Ctrl+D to quit)
Session: cli-1741500000

mose>
```

Type a message and confirm you get a response. Tool calls print inline.

### Run the test suite

```bash
source .venv/bin/activate
pytest tests/ -v
```

### Check the services

```bash
# Status
sudo systemctl status worker-agent
sudo systemctl status mose-agent

# Live logs
journalctl -u mose-agent -f

# Structured log inspection
jq '.' /home/Mose/mose-agent/data/logs/mose-*.jsonl | head -50
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `CUDA error: no kernel image` | Driver/CUDA version mismatch | Update NVIDIA driver to 570+ and reinstall vLLM for CUDA 13.0 |
| `Connection refused` on port 8001 | worker-agent not running | `sudo systemctl start worker-agent` and check `journalctl -u worker-agent` |
| `Permission denied` on GPU | Mose user not in video/render groups | `sudo usermod -aG video,render Mose` then log out/in |
| `ModuleNotFoundError` | venv not activated or package missing | `source .venv/bin/activate && pip install -e .` |
| Empty LLM responses | Model still loading | Wait for vLLM to report ready in `journalctl -u worker-agent` |
| `OutOfMemoryError` during startup | GPU memory exhausted | Reduce `--gpu-memory-utilization` or `--max-model-len` |
| `OutOfMemoryError` during CUDA graph capture | Graph capture too large for 12GB cards | Reduce `cudagraph_capture_sizes` in the `-O` flag (e.g. `[1, 2, 4]`) |
| `mcp_servers.json` not found | File is gitignored | Copy from `mcp_servers.example.json` (see Section 7) |
| `DISCORD_TOKEN` not set | Missing `.env` or env var | Set in `.env` or export before running |
| SQLite errors on first run | `data/` directory missing | `mkdir -p data/{logs,workspace,tool_outputs}` |
| Signal bot won't connect | signal-cli not linked or daemon down | Check `systemctl status signal-cli-daemon` and re-link if needed |

---

## Directory and File Reference

```
~/mose-agent/                          # /home/Mose/mose-agent
├── .env                               # Secrets (chmod 600, gitignored)
├── .env.example                       # Template for .env
├── config.toml                        # Agent configuration
├── mcp_servers.json                   # MCP server registry (gitignored)
├── mcp_servers.example.json           # Template for mcp_servers.json
├── pyproject.toml                     # Python package definition
├── mose-agent.service                 # systemd unit — the agent
├── worker-agent.service               # systemd unit — vLLM LLM server
├── signal-cli-daemon.service          # systemd unit — signal-cli JSON-RPC
├── mose/                              # Source code
│   ├── __main__.py                    # Entry point (python -m mose)
│   ├── agent.py                       # Core agent loop
│   ├── llm.py                         # LLM client
│   ├── memory.py                      # Persistent memory (SQLite + vectors)
│   ├── tools.py                       # Native tools (bash, files, web, etc.)
│   ├── tool_output.py                 # Large output handling pipeline
│   ├── mcp_manager.py                 # MCP tool client
│   ├── discord_bot.py                 # Discord interface
│   ├── signal_bot.py                  # Signal interface
│   ├── observe.py                     # Structured JSON logging
│   └── config.py                      # Config loader
├── skills/                            # Agent skill files (homelab knowledge)
├── tests/                             # Test suite
└── data/                              # Runtime data (gitignored, create manually)
    ├── memory.db                      # SQLite database
    ├── logs/                          # mose-YYYY-MM-DD.jsonl
    ├── workspace/                     # Tool sandbox
    └── tool_outputs/                  # Persisted large outputs
```

---

## Security Notes

- The `Mose` user has **no sudo rights**. All agent processes run unprivileged.
- The `bash` tool blocks destructive commands (`rm -rf /`, `mkfs`, `shutdown`, etc.).
- File writes are sandboxed to `data/workspace/` — the agent cannot write outside it.
- The `sre_execute` tool requires human approval (via Discord reaction or CLI prompt) before running state-changing commands.
- `.env` should be `chmod 600` — it contains API keys and tokens.
- `mcp_servers.json` is gitignored to prevent leaking server configurations.
- vLLM binds to `0.0.0.0:8001` — restrict with a firewall if the host is network-exposed.
