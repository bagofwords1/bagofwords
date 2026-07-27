# Docker Compose Auto-Update

A shell script that automatically pulls the latest Docker images and recreates containers without downtime. Designed to run as a cron job.

## How It Works

1. Pulls the latest images for all services defined in the compose file
2. Recreates only the containers whose images have changed (zero downtime)
3. Prunes unused dangling images to free disk space

## Usage

```bash
./version-auto-update.sh -c <compose-command> -f <docker-compose-file>
```

### Parameters

| Flag | Description                 | Example                                   |
|------|-----------------------------|-------------------------------------------|
| `-c` | Docker compose command      | `"docker compose"` or `"docker-compose"`  |
| `-f` | Path to docker-compose.yaml | `/opt/myapp/docker-compose.yaml`          |

### Examples

```bash
./version-auto-update.sh -c "docker compose" -f /opt/myapp/docker-compose.yaml
./version-auto-update.sh -c "docker-compose" -f /opt/myapp/docker-compose.yaml
```

## Setup

### 1. Make the script executable

```bash
chmod +x version-auto-update.sh
```

### 2. Add a cron job

Open the crontab editor:

```bash
# For current user
crontab -e

# For root (if Docker requires root access)
sudo crontab -e
```

Add the following line to run twice a day (6:00 AM and 6:00 PM):

```
0 6,18 * * * /full/path/to/auto-update.sh -c "docker compose" -f /full/path/to/docker-compose.yaml >> /var/log/auto-update.log 2>&1
```

Save and exit the editor.

### 3. Verify the cron job

```bash
crontab -l
```

## Cron Schedule Examples

| Schedule                 | Cron Expression |
|--------------------------|-----------------|
| Twice a day (6am, 6pm)   | `0 6,18 * * *`  |
| Every 6 hours            | `0 */6 * * *`   |
| Once a day at midnight   | `0 0 * * *`     |
| Every hour               | `0 * * * *`     |

## Logs

All output is appended to `/var/log/auto-update.log`. To monitor:

```bash
tail -f /var/log/version-auto-update.log
```

## Notes

- Cron jobs are persistent and survive server reboots.
- The `cron` daemon starts automatically on boot.
- `docker compose up -d` only recreates containers whose images have changed, so services with unchanged images experience no interruption.
