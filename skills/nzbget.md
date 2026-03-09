# NZBGet (Usenet Downloader)

## Connection

- Port: 6789 (web UI)
- JSON-RPC: http://<host>:6789/jsonrpc
- Password env var: `NZBGET_PASSWORD`
- Auth: Basic auth or `Username: nzbget` + `Password: $NZBGET_PASSWORD`

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### Status
```bash
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"status"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"
```

### List Groups (Queue)
```bash
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"listgroups"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"
```

### History
```bash
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"history"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"
```

### Log
```bash
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"log"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"
```

### Config
```bash
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"config"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"
```

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Pause/Resume
```bash
# Pause
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"pausedownload"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"

# Resume
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"resumedownload"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"
```

### Edit Queue
```bash
# Use editqueue method - consult NZBGet JSON-RPC docs
```

### Reload
```bash
curl -s -u "nzbget:$NZBGET_PASSWORD" \
  -d '{"method":"reload"}' \
  -H "Content-Type: application/json" \
  "http://localhost:6789/jsonrpc"
```

### Restart Service
```bash
systemctl restart nzbget
# or: docker restart nzbget
```
