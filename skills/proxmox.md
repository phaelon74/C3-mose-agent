# Proxmox VE

## Connection

- Web UI: https://proxmox-host:8006/
- CLI: `qm` (VMs), `pct` (containers), `pvesh` (API)
- API token env vars: `PROXMOX_API_TOKEN_ID`, `PROXMOX_API_TOKEN_SECRET`
- Config: `/etc/pve/`

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### List VMs
```bash
qm list
```

### VM Status
```bash
qm status <vmid>
```

### List Containers
```bash
pct list
```

### Node Info
```bash
pvesh get /nodes
pvesh get /cluster/resources
```

### Version
```bash
cat /etc/pve/.version
```

### Storage
```bash
zpool status
zfs list
```

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### VM Control
```bash
qm start <vmid>
qm stop <vmid>
qm reboot <vmid>
```

### Container Control
```bash
pct start <ctid>
pct stop <ctid>
```

### Backup
```bash
vzdump <vmid>
```

### Snapshot
```bash
qm snapshot <vmid> <snapname>
```

### Migrate
```bash
qm migrate <vmid> <target>
```
