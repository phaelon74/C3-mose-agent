# Migrating Mose to a new machine

Move a Docker Compose Mose deployment from the current host to a new Linux machine. Run the commands below on the hosts, in order. The agent user on both machines is `mose`, and the checkout path on both machines is `/home/mose/mose-agent`. The systemd units use that path.

The first time the containers start on the new machine is the last step. Starting them earlier creates empty Docker volumes, and the agent writes a blank memory database.

## What the backup contains

`scripts/mose-backup.sh` writes one archive, mode `600`, under `/home/mose/mose-backups/`. It holds:

- `.env` (API keys and the other secrets)
- `mcp_servers.json`, `mcp_servers.portal.json`, and `config.toml`
- `docker-compose.override.yml`, if you have one
- Docker volumes `mose-data` (SQLite `memory.db` and `upcoming.db`, logs, tool outputs), `mose-workspace`, and `mose-skills`
- Signal device data from `/home/mose/.local/share/signal-cli`
- installed systemd units for `mose-*`, `signal-cli-daemon`, and `worker-agent`

Memory is SQLite inside the `mose-data` volume. There is no separate database server to start.

## What the backup does not contain

- the git checkout (clone it on the new machine)
- Docker images (the restore script builds them)
- `/opt/signal-cli` (pack that tarball separately)
- the model server listening on port `8080`

Current model setting:

```text
LLM_ENDPOINT=http://172.17.0.1:8080/v1
```

`172.17.0.1` is the Docker bridge address of the host. `config.toml` also uses that address so the container can reach signal-cli (`daemon_host = "172.17.0.1"`, port `7583`).

signal-cli on this deployment is the native binary **0.14.5** (`ELF 64-bit`). The new machine does not need Java.

## 1. On the current machine, stop writers and take the backup

`scripts/mose-backup.sh` will not prompt for a sudo password. Stop Signal and the prune timer yourself first, so those copies are taken while nothing is writing them.

```bash
sudo systemctl stop signal-cli-daemon
sudo systemctl stop mose-docker-prune.timer
cd /home/mose/mose-agent
bash scripts/mose-backup.sh
```

`sudo` asks for a password here. The script stops the Compose containers if they are running, archives the volumes, and starts those containers again when it finishes. It prints a line like:

```text
Wrote /home/mose/mose-backups/mose-backup-YYYYMMDDTHHMMSSZ.tar.gz
```

Use that new file. An archive taken while `signal-cli-daemon` was still running (including `mose-backup-20260921T214256Z.tar.gz`) can have a half-written Signal database.

Pack the signal-cli install:

```bash
sudo tar -C /opt -czf /home/mose/mose-backups/signal-cli-install.tar.gz signal-cli
sudo chown mose:mose /home/mose/mose-backups/signal-cli-install.tar.gz
ls -lh /home/mose/mose-backups
```

Record the git commit the running tree is on:

```bash
git -C /home/mose/mose-agent rev-parse HEAD
```

See which program owns the model port, so you know what else has to move:

```bash
sudo ss -ltnp | grep 8080
```

## 2. On the new machine, install Docker and the mose user

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo useradd -m -s /bin/bash mose
sudo usermod -aG docker mose
sudo passwd mose
sudo -u mose mkdir -p /home/mose/mose-backups
sudo -u mose chmod 700 /home/mose/mose-backups
```

Log in once so the `docker` group applies, confirm it, then disconnect:

```bash
ssh mose@NEW_IP
id
exit
```

`id` must include `docker`. Replace `NEW_IP` with the new machine's address in every later `scp` and `ssh`.

## 3. Copy the archive and signal-cli onto the new machine

On the current machine, as `mose`, copy the new backup only (not an older one taken while Signal was running):

```bash
scp /home/mose/mose-backups/mose-backup-YYYYMMDDTHHMMSSZ.tar.gz /home/mose/mose-backups/signal-cli-install.tar.gz mose@NEW_IP:/home/mose/mose-backups/
```

## 4. On the new machine, clone the repo and restore

SSH in as `mose`:

```bash
ssh mose@NEW_IP
cd /home/mose
git clone https://github.com/phaelon74/C3-mose-agent.git mose-agent
cd /home/mose/mose-agent
git checkout PASTE_THE_COMMIT_FROM_STEP_1
```

The clone has to stay at `/home/mose/mose-agent`.

From the current machine, copy the restore scripts into that clone. Do this even if you also checked out a commit, so the new machine has the same scripts that created the archive:

```bash
scp /home/mose/mose-agent/scripts/mose-restore.sh /home/mose/mose-agent/scripts/mose-migrate-common.sh mose@NEW_IP:/home/mose/mose-agent/scripts/
```

On the new machine:

```bash
cd /home/mose/mose-agent
BACKUP=$(ls -1 /home/mose/mose-backups/mose-backup-*.tar.gz)
echo "$BACKUP"
bash scripts/mose-restore.sh "$BACKUP"
```

Wait until that command exits. It does four things, in this order:

1. Copies `.env`, the MCP registries, and `config.toml` into the clone.
2. Rewrites `DOCKER_GID` in `.env` to this machine's `docker` group. The old value is wrong here.
3. Runs `docker compose build`. The Dockerfile copies `mcp_servers.json`, `mcp_servers.portal.json`, and `config.toml` into the image, so the build has to happen after those files are in the clone.
4. Creates the named volumes and unpacks memory, workspace, and skills.

It does not start the stack.

Install signal-cli from the tarball:

```bash
sudo tar -C /opt -xzf /home/mose/mose-backups/signal-cli-install.tar.gz
/opt/signal-cli/bin/signal-cli --version
```

The version line should be `signal-cli 0.14.5`.

Confirm the Docker bridge address the containers will use to reach this host:

```bash
ip -4 addr show docker0
```

A default Docker install shows `172.17.0.1`. That matches the restored `LLM_ENDPOINT` and `daemon_host`. If the address is different, edit both before the containers start:

- `LLM_ENDPOINT` in `/home/mose/mose-agent/.env` (host and port of the model server)
- `daemon_host` under `[signal]` in `/home/mose/mose-agent/config.toml`

`daemon_host` is read from `config.toml` inside the image, so after changing that file rebuild the agent image:

```bash
cd /home/mose/mose-agent
docker compose build mose-agent
```

## 5. Stop Mose on the current machine

Do this before Signal starts on the new machine. Two copies of the same linked device will conflict.

On the current machine:

```bash
cd /home/mose/mose-agent
docker compose stop
sudo systemctl stop signal-cli-daemon
sudo systemctl stop mose-skill-review.timer mose-upcoming-sync.timer mose-docker-prune.timer
```

Leave the model server running if it is staying on this machine and the new Mose will keep calling it. If the model server is moving with Mose, leave it up until the same program is listening on port `8080` on the new machine.

## 6. Restore Signal and systemd on the new machine

On the new machine:

```bash
sudo bash /home/mose/mose-backups/host-state/mose-host-restore.sh
```

That script restores `/home/mose/.local/share/signal-cli`, copies the systemd units into `/etc/systemd/system`, and starts `signal-cli-daemon` plus the timers that were enabled on the old host. It copies `worker-agent.service` and does not enable it.

## 7. Bring the model server up

Mose calls `http://172.17.0.1:8080/v1` on the Docker host. The backup does not include that program or its weights.

If that server is staying on the old machine, point `LLM_ENDPOINT` at an address the new host can reach, then recreate the agent after the edit:

```bash
cd /home/mose/mose-agent
docker compose up -d
```

If that server is moving to the new machine, install and start the same program `ss` showed on port `8080`, and confirm something is listening before you start Mose:

```bash
sudo ss -ltnp | grep 8080
```

## 8. Start Mose on the new machine

```bash
cd /home/mose/mose-agent
docker compose up -d
docker compose ps
docker compose logs -f mose-agent
```

Ctrl+C stops following the log. The containers keep running.

Check the agent user and Signal:

```bash
docker compose exec mose-agent id
systemctl status signal-cli-daemon
```

`id` inside the container should show uid `1000` and a `docker` group. `signal-cli-daemon` should be active. Send a message in the engagement Signal group and confirm Mose answers.
