# Plex Media Server

## Connection

- Port: 32400
- API: http://<host>:32400/
- Token env var: `PLEX_TOKEN`
- Header: `X-Plex-Token: $PLEX_TOKEN`
- Logs: `/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Logs/` (Linux) or container volume

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### Sessions (Active Streams)
```bash
curl -s -H "X-Plex-Token: $PLEX_TOKEN" \
  "http://localhost:32400/status/sessions"
```

### Library Sections
```bash
curl -s -H "X-Plex-Token: $PLEX_TOKEN" \
  "http://localhost:32400/library/sections"
```

### Section Content
```bash
curl -s -H "X-Plex-Token: $PLEX_TOKEN" \
  "http://localhost:32400/library/sections/<id>/all"
```

### Logs
```bash
tail -100 "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log"
```

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Scan Library
```bash
curl -s -X POST -H "X-Plex-Token: $PLEX_TOKEN" \
  "http://localhost:32400/library/sections/<id>/refresh"
```

### Optimize Database
```bash
curl -s -X PUT -H "X-Plex-Token: $PLEX_TOKEN" \
  "http://localhost:32400/library/optimize"
```

### Restart Service
```bash
systemctl restart plexmediaserver
# or: docker restart plex
```
