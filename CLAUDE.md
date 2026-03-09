# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

This is a **Komodo stack library** — a Git-based source of truth for Docker Compose stack definitions deployed via [Komodo](https://komo.do/) to the `r720-omv` server (`10.10.1.13`).

## Repository Structure

```
r720-omv/<stack-name>/compose.yaml   # Stack definitions (one per directory)
servers.toml                          # Komodo server configuration
stacks.toml                           # Stack metadata and environment variable mappings
compose.yaml                          # Root-level compose (special/test stacks only)
```

## Deployment Workflow

1. Edit the `compose.yaml` file in the appropriate `r720-omv/<stack>/` directory
2. Commit and push to GitHub
3. Manually trigger deploy in Komodo UI

**Never edit files directly on the server.**

## Compose File Conventions

### Image Versioning
Use `x-versions` anchors at the top of the file for pinned image versions:
```yaml
x-versions:
  grafana: &grafana-version "grafana/grafana:12.4.0"

services:
  grafana:
    image: *grafana-version
```

### Security Hardening (apply to all services)
```yaml
ipc: private
restart: unless-stopped
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
user: "${DOCKER_PUID}:${DOCKER_PGID}"
```

### Required Labels
```yaml
labels:
  wud.watch: "true"                                    # What's Up Docker monitoring
  wud.tag.include: '^\d+\.\d+\.\d+$$'                  # Version regex
  wud.display.name: "Service Name"
  wud.link.template: 'https://github.com/org/repo/releases/tag/v$${major}.$${minor}.$${patch}'
```

### Logging
```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

### Volumes
- Bind mounts to `/opt/docker/<stack>/<container>/`
- Include timezone: `/etc/localtime:/etc/localtime:ro` or use `TZ` env var

### Networking
- Join external `traefik` network for exposed services
- Use Traefik labels for routing — never publish ports directly for web services
- Internal services use stack-specific bridge networks

## Environment Variables

All secrets and configuration are managed in Komodo using `[[VARIABLE_NAME]]` syntax in `stacks.toml`. Common global variables:
- `${TZ}` — Timezone
- `${DOCKER_PUID}` / `${DOCKER_PGID}` — Container user/group IDs
- `${DOCKER_SOCKET_GID}` — Docker socket group
- `${DOMAIN_NAME}` — Base domain for services

## CI/CD

GitHub Actions workflow (`.github/workflows/docker-compose-check.yml`) runs on push/PR:
- YAML linting with yamllint
- Docker Compose syntax validation
- Trivy security scan for HIGH/CRITICAL issues

## Persistent Data Paths

All container data lives on the server at:
```
/opt/docker/<stack>/<container>/
    ├── config/
    ├── data/
    └── logs/
```
