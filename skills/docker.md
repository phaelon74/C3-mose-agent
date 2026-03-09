# Docker (Pulsarr, Huntarr, Homarr)

## Connection

- Pulsarr: port 3003 (webhook integration for Plex watchlist → Sonarr/Radarr)
- Huntarr: port 9705 (automated media search/upgrade)
- Homarr: port 7575 (dashboard)
- Containers may be named: pulsarr, huntarr, homarr (or as defined in compose)

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### List Containers
```bash
docker ps
docker ps -a
```

### Container Logs
```bash
docker logs <container_name> --tail 100
docker logs -f <container_name>
```

### Container Stats
```bash
docker stats --no-stream
```

### Inspect Container
```bash
docker inspect <container_name>
```

### Compose Status
```bash
docker compose ps
```

### Check Health
```bash
curl -s http://localhost:3003/  # Pulsarr
curl -s http://localhost:9705/  # Huntarr
curl -s http://localhost:7575/  # Homarr
```

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Restart Container
```bash
docker restart <container_name>
```

### Restart All (Compose)
```bash
docker compose restart
```

### Pull and Recreate
```bash
docker compose pull && docker compose up -d
```

### Prune Unused
```bash
docker system prune -f
```

### Stop/Start Compose Stack
```bash
docker compose down
docker compose up -d
```
