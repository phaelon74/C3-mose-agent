# OPNsense Firewall

## Connection

- Web UI: typically https://opnsense-host/
- API: https://opnsense-host/api/
- API key env vars: `OPNSENSE_API_KEY`, `OPNSENSE_API_SECRET`
- Auth: API key + secret in request headers or basic auth

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### System Status
```bash
curl -s -u "$OPNSENSE_API_KEY:$OPNSENSE_API_SECRET" \
  "https://<opnsense-host>/api/core/system/status"
```

### Interface Statistics
```bash
curl -s -u "$OPNSENSE_API_KEY:$OPNSENSE_API_SECRET" \
  "https://<opnsense-host>/api/diagnostics/interface/getInterfaceStatistics"
```

### Firewall Rules
```bash
curl -s -u "$OPNSENSE_API_KEY:$OPNSENSE_API_SECRET" \
  "https://<opnsense-host>/api/firewall/filter/searchRule"
```

### View Logs
Check OPNsense web UI: System > Log Files > Backend, or via API if available.

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Add/Modify Firewall Rule
API POST to `/api/firewall/filter/` — consult OPNsense API docs for payload format.

### Restart Services
```bash
# Via API or SSH to OPNsense host
```

### Apply Config
Changes often require applying config from the web UI or API.
