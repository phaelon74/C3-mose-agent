# DrivePool (StableBit - Windows)

## Connection

- Runs on a separate Windows machine
- Access from Linux via `pypsrp` (PowerShell Remoting Protocol)
- Env vars: `DRIVEPOOL_HOST`, `DRIVEPOOL_USER`, `DRIVEPOOL_PASSWORD`
- Requires: WinRM enabled on Windows, `Enable-PSRemoting`

## ReadOnly Runbooks

Use `bash` for these — no approval required. Commands run via pypsrp from Linux.

### Pool Status
```bash
python3 -c "
from pypsrp.client import Client
client = Client('$DRIVEPOOL_HOST', username='$DRIVEPOOL_USER', password='$DRIVEPOOL_PASSWORD', ssl=False)
output, streams, had_errors = client.execute_ps('Get-Pool')
print(output)
"
```

### Physical Disks
```bash
python3 -c "
from pypsrp.client import Client
client = Client('$DRIVEPOOL_HOST', username='$DRIVEPOOL_USER', password='$DRIVEPOOL_PASSWORD', ssl=False)
output, streams, had_errors = client.execute_ps('Get-PhysicalDisk')
print(output)
"
```

### Volume Info
```bash
python3 -c "
from pypsrp.client import Client
client = Client('$DRIVEPOOL_HOST', username='$DRIVEPOOL_USER', password='$DRIVEPOOL_PASSWORD', ssl=False)
output, streams, had_errors = client.execute_ps('Get-Volume')
print(output)
"
```

Note: StableBit DrivePool uses custom PowerShell cmdlets. Adjust cmdlet names per StableBit documentation (e.g., `Get-StoragePool`, `Get-PhysicalDisk` for generic Windows storage).

## Execute Runbooks

Use `sre_execute` for these — requires human approval. **Extremely sensitive** — pool rebalance, add/remove drive can affect data.

### Rebalance Pool
```bash
# StableBit-specific - consult docs for exact cmdlet
python3 -c "
from pypsrp.client import Client
client = Client('$DRIVEPOOL_HOST', username='$DRIVEPOOL_USER', password='$DRIVEPOOL_PASSWORD', ssl=False)
client.execute_ps('Invoke-StoragePoolRebalance')  # example - verify cmdlet
"
```

### Add/Remove Drive
Requires StableBit DrivePool PowerShell module. Always use `sre_execute` with explicit human approval.
