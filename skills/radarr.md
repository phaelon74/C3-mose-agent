# Radarr (Movie Management)

## Connection

- Port: 7878
- API: http://<host>:7878/api/v3/
- API key env var: `RADARR_API_KEY`
- Header: `X-Api-Key: $RADARR_API_KEY`
- Config/logs: `/var/lib/radarr/` or container volume

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### Health Check
```bash
curl -s -H "X-Api-Key: $RADARR_API_KEY" "http://localhost:7878/api/v3/health"
```

### System Status
```bash
curl -s -H "X-Api-Key: $RADARR_API_KEY" "http://localhost:7878/api/v3/system/status"
```

### Queue
```bash
curl -s -H "X-Api-Key: $RADARR_API_KEY" "http://localhost:7878/api/v3/queue"
```

### Movies
```bash
curl -s -H "X-Api-Key: $RADARR_API_KEY" "http://localhost:7878/api/v3/movie"
```

### Logs
Check `/var/lib/radarr/logs/` or container logs.

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Trigger Search
```bash
curl -s -X POST -H "X-Api-Key: $RADARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"MoviesSearch"}' \
  "http://localhost:7878/api/v3/command"
```

### Refresh Movie
```bash
curl -s -X POST -H "X-Api-Key: $RADARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"RefreshMovie","movieIds":[<id>]}' \
  "http://localhost:7878/api/v3/command"
```

### Delete Movie
```bash
curl -s -X DELETE -H "X-Api-Key: $RADARR_API_KEY" \
  "http://localhost:7878/api/v3/movie/<id>"
```

### Restart Service
```bash
systemctl restart radarr
# or: docker restart radarr
```
