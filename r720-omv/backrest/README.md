# Backrest

Web-UI orchestrator around **restic** — backs up the container bind-mount data under
`/opt/docker` with deduplication + compression, browsable/restorable from the UI.
Replaces the abandoned-for-bind-mounts repliqate approach: Backrest backs up by **path**,
so bind mounts need no `o: bind` volume tricks.

- **URL:** `http://10.10.1.13:9898` (direct host port, no reverse proxy)
- **Engine:** restic (repo is plain restic — readable by the `restic` CLI, no lock-in)
- **Image:** `garethgeorge/backrest:*-alpine` — the `-alpine` tag is **required** (the
  default `scratch` image has no `curl`/`bash`/`docker-cli`, which the hooks need)

## Komodo environment variables

All already defined for other stacks — no new ones needed:

| Variable             | Used for                                          |
|----------------------|---------------------------------------------------|
| `DOMAIN_NAME`        | Traefik router host                               |
| `DOCKER_SOCKET_GID`  | Socket-proxy group                                |
| `TZ`                 | Timezone                                          |
| `SIGNAL_PHONE_NUMBER`| Signal sender (signal-rest-api) — failure hook    |
| `SIGNAL_TECH_ID`     | Signal recipient — failure hook                   |

The **restic repository password** is set in the Backrest UI when you create the repo
(below) — it is NOT an env var. **Store it in your password manager; losing it makes the
backups unrecoverable.**

## Host directories

Docker auto-creates bind sources, but for clarity these are used (owned by root, since
Backrest runs as root to read every UID's files):

```
/opt/docker/backrest/config/      # config.json (BACKREST_CONFIG)
/opt/docker/backrest/data/        # oplog / internal state (BACKREST_DATA)
/opt/docker/backrest/cache/       # restic cache (XDG_CACHE_HOME) — excluded from backup
/opt/docker/db-dumps/             # DB dumps written by the pre-backup hook (BACKED UP)
/Data/IT/Docker_backups/backrest/ # the restic repository (the actual backups)
```

## First-run setup (Backrest UI)

### 1. Create the repository
- **Repo URI:** `/repos`  (local restic repo at `/Data/IT/Docker_backups/backrest`)
- **Password:** generate a strong one → save it offline.
- Compression: `auto` (zstd). Optionally set `RESTIC_COMPRESSION=max` for config-heavy data.

### 2. Create one plan: `opt-docker`
- **Path:** `/userdata`  (this is `/opt/docker` mounted read-only)
- **Schedule:** cron `0 3 * * *` (daily 03:00)
- **Retention:** keep daily 7, weekly 4, monthly 6 (tune to taste)
- **Excludes** (regenerable / self / junk):
  ```
  backrest
  lost+found
  **/cache
  **/valkey-data
  **/redis-data
  romm/romm_resources
  ```
  (`backrest` = Backrest's own config/data/cache; `db-dumps` is intentionally NOT excluded.)

### 3. Hook — pre-backup DB dumps  →  attach to plan `opt-docker`
- **Condition:** `CONDITION_SNAPSHOT_START`
- **Error behavior:** `ON_ERROR_FATAL` (a failed dump fails the run, which fires the Signal alert)
- **Command:**
  ```bash
  #!/bin/bash
  set -uo pipefail
  mkdir -p /dumps

  # Paperless-ngx (Postgres) — creds read from the target container's own env
  docker exec paperlessngx-postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > /dumps/paperlessngx-postgres.sql

  # Pricebuddy (MariaDB)
  docker exec pricebuddy-mariadb sh -c \
    'mariadb-dump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --events --all-databases' \
    > /dumps/pricebuddy-mariadb.sql

  # Romm (MariaDB)
  docker exec romm-mariadb sh -c \
    'mariadb-dump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --events --all-databases' \
    > /dumps/romm-mariadb.sql
  ```
  Dumps land in `/dumps` (= `/opt/docker/db-dumps` = `/userdata/db-dumps`), so the very
  same snapshot captures both the live data dirs AND a consistent logical dump of each DB.

### 4. Hook — Signal notification on failure  →  attach to plan (or repo, for all plans)
- **Conditions:** `CONDITION_SNAPSHOT_ERROR`, `CONDITION_ANY_ERROR`
  (add `CONDITION_SNAPSHOT_SUCCESS` if you want success pings too)
- **Error behavior:** `ON_ERROR_IGNORE`
- **Command:**
  ```bash
  #!/bin/bash
  curl -fsS -X POST "http://signal-rest-api:8080/v2/send" \
    -H "Content-Type: application/json" \
    --data '{"message": {{ .JsonMarshal .Summary }}, "number": "'"${SIGNAL_PHONE_NUMBER}"'", "recipients": ["'"${SIGNAL_TECH_ID}"'"]}'
  ```
  `{{ .JsonMarshal .Summary }}` emits a properly JSON-escaped, multi-line event summary.
  `number`/`recipients` come from the container env (the Komodo `SIGNAL_*` vars).

## Restoring

- **Files:** Backrest UI → snapshot → browse → restore (or download). Or mount the repo:
  `docker exec -it backrest restic -r /repos mount /mnt` (FUSE), or `restic` CLI on any box.
- **A database:** restore the dump file, then replay it, e.g.
  ```bash
  cat paperlessngx-postgres.sql | docker exec -i paperlessngx-postgres \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
  ```

## Notes
- Backrest runs as **root + cap `DAC_READ_SEARCH`** (read-only DAC bypass) so it can read
  every file under `/opt/docker` regardless of owner/mode, without write-bypass.
- Hooks reach the Docker API through `backrest-socket-proxy` (read-only socket; only
  `containers/*` + `exec/*` endpoints) — Backrest never touches the raw socket.
- The `db-dumps` approach means the DB containers are **not stopped** during backup
  (no downtime); consistency comes from the logical dump, not a cold copy.
