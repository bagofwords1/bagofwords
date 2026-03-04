# Deployment

## Docker

```bash
docker run --pull always -d -p 3000:3000 walrusquant/metricchat
```

Uses SQLite by default. For PostgreSQL, set `MC_DATABASE_URL`:

```bash
docker run --pull always -d -p 3000:3000 \
  -e MC_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname \
  walrusquant/metricchat
```

## Docker Compose

Production deployment with PostgreSQL and Caddy (automatic TLS).

```bash
git clone https://github.com/metricchat/metricchat.git
cd metricchat
```

Create a `.env` file:

```env
DOMAIN=your-domain.com
POSTGRES_USER=metricchat
POSTGRES_PASSWORD=your-secure-password
POSTGRES_DB=metricchat
MC_DATABASE_URL=postgresql+asyncpg://metricchat:your-secure-password@postgres:5432/metricchat
MC_ENCRYPTION_KEY=your-fernet-key
```

Generate an encryption key:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Start services:

```bash
docker compose up -d
```

Point your domain's A record to the server IP. Caddy handles TLS automatically.

For local development without SSL:

```bash
docker compose -f docker-compose.dev.yaml up -d
```

## Configuration

Mount a `metricchat.yaml` for settings like OAuth, SMTP, and feature flags:

```bash
docker run --pull always -d -p 3000:3000 \
  -v $(pwd)/metricchat.yaml:/app/metricchat.yaml \
  walrusquant/metricchat
```

## Kubernetes / Helm

```bash
helm upgrade -i --create-namespace -n metricchat metricchat metricchat/metricchat
```

Supports bundled PostgreSQL, external managed databases (Aurora, RDS), IAM auth, and custom TLS.

## Environment Variables

| Variable | Description |
|---|---|
| `MC_DATABASE_URL` | PostgreSQL or SQLite connection string |
| `MC_ENCRYPTION_KEY` | Fernet key for credential encryption (must persist across restarts) |
| `ENVIRONMENT` | `development`, `staging`, or `production` |

## Full Documentation

See [metricchat.com/docs](https://www.metricchat.com/docs) for complete deployment guides, authentication setup (Google OAuth, OIDC), and AWS Aurora integration.
