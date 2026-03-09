# Sonarr (TV Management)

## Connection

- Port: 8989
- API: http://<host>:8989/api/v3/
- API key env var: `SONARR_API_KEY`
- Header: `X-Api-Key: $SONARR_API_KEY`
- Config/logs: `/var/lib/sonarr/` or container volume

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### Health Check
```bash
curl -s -H "X-Api-Key: $SONARR_API_KEY" "http://localhost:8989/api/v3/health"
```

### System Status
```bash
curl -s -H "X-Api-Key: $SONARR_API_KEY" "http://localhost:8989/api/v3/system/status"
```

### Queue
```bash
curl -s -H "X-Api-Key: $SONARR_API_KEY" "http://localhost:8989/api/v3/queue"
```

### Series
```bash
curl -s -H "X-Api-Key: $SONARR_API_KEY" "http://localhost:8989/api/v3/series"
```

### Logs
Check `/var/lib/sonarr/logs/` or container logs.

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Trigger Search
```bash
curl -s -X POST -H "X-Api-Key: $SONARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"SeriesSearch"}' \
  "http://localhost:8989/api/v3/command"
```

### Refresh Series
```bash
curl -s -X POST -H "X-Api-Key: $SONARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"RefreshSeries","seriesIds":[<id>]}' \
  "http://localhost:8989/api/v3/command"
```

### Delete Series
```bash
curl -s -X DELETE -H "X-Api-Key: $SONARR_API_KEY" \
  "http://localhost:8989/api/v3/series/<id>"
```

### Restart Service
```bash
systemctl restart sonarr
# or: docker restart sonarr
```
