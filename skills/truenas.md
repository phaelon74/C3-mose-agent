# TrueNAS (Storage)

## Connection

- Web UI: https://truenas-host/
- API: https://truenas-host/api/v2.0/
- API key env var: `TRUENAS_API_KEY`
- Header: `Authorization: Bearer $TRUENAS_API_KEY`
- CLI: `midclt` (on TrueNAS host), `zpool`, `zfs` (if SSH access)

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### Pool Status
```bash
curl -s -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<truenas-host>/api/v2.0/pool"
```

### Datasets
```bash
curl -s -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<truenas-host>/api/v2.0/pool/dataset"
```

### SMB Shares
```bash
curl -s -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<truenas-host>/api/v2.0/sharing/smb"
```

### NFS Shares
```bash
curl -s -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<truenas-host>/api/v2.0/sharing/nfs"
```

### Disk Status
```bash
curl -s -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<truenas-host>/api/v2.0/disk"
```

### Alerts
```bash
curl -s -H "Authorization: Bearer $TRUENAS_API_KEY" \
  "https://<truenas-host>/api/v2.0/alert/list"
```

### ZFS (if SSH)
```bash
zpool status
zfs list
```

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Create Snapshot
```bash
curl -s -X POST -H "Authorization: Bearer $TRUENAS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dataset":"pool/dataset","name":"snap-name"}' \
  "https://<truenas-host>/api/v2.0/pool/snapshot"
```

### Scrub Pool
```bash
curl -s -X POST -H "Authorization: Bearer $TRUENAS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"pool_id":<id>}' \
  "https://<truenas-host>/api/v2.0/pool/scrub"
```

### Add/Modify Share
API POST to `/api/v2.0/sharing/smb` or `/api/v2.0/sharing/nfs` — consult TrueNAS API docs.
