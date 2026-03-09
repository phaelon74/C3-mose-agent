# Linux (Ubuntu)

## Connection

Mose typically runs on the same Ubuntu host or can SSH to other hosts. Commands run in the workspace by default.

## ReadOnly Runbooks

Use `bash` for these — no approval required.

### Check Service Status
```bash
systemctl status <service>
```

### View Logs
```bash
journalctl -u <service> --no-pager -n 50
journalctl -f -u <service>  # follow
```

### Disk Usage
```bash
df -h
du -sh /path
```

### Memory
```bash
free -m
```

### Network
```bash
ip a
ss -tlnp
```

### Package Updates Available
```bash
apt list --upgradable
```

### System Info
```bash
uptime
top -bn1
cat /etc/os-release
```

### Common Log Paths
- `/var/log/syslog`
- `/var/log/auth.log`
- Application logs often in `/var/log/<app>/` or `/var/lib/<app>/logs/`

## Execute Runbooks

Use `sre_execute` for these — requires human approval.

### Restart Service
```bash
systemctl restart <service>
```

### Package Updates
```bash
apt update && apt upgrade -y
```

### Firewall (UFW)
```bash
ufw allow <port>
ufw deny <port>
ufw reload
```

### User Management
```bash
useradd ...
usermod ...
```

### Reboot
```bash
reboot
```
