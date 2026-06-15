# Deploying Castor on a server (Docker)

Self-hosted, single container, **persistent memory**. All state lives in one
mounted folder (`./castor-data`) so nothing is lost across restarts or upgrades.

## What persists

Everything under the container's `/data` (mapped to `./castor-data` on the
host):

| Path | What |
|---|---|
| `castor.db` | SQLite — threads, messages, settings, secrets, cron |
| `memory/` | Qdrant vectors (disk mode) — the knowledge graph + recall |
| `wiki/` | synthesized markdown pages |
| `skills/` | user-dropped skills |
| `uploads/` | images, docs, camera/TTS files |
| `workspace/` | default working dir for the agent |
| `presets/`, `logs/`, `backups/` | presets, logs, db backups |

Back up the whole install by copying `./castor-data`.

---

## Quick start (prebuilt image — recommended)

Requires Docker + the Compose plugin on the server.

```bash
# 1. Get the deploy files (clone, or copy docker-compose.yml + .env.example)
git clone https://github.com/deepfounder-ai/castor.git
cd castor

# 2. Configure
cp .env.example .env
nano .env          # set CASTOR_LLM_URL / _MODEL / _KEY and a CASTOR_PASSWORD

# 3. Launch
docker compose up -d

# 4. Check it
docker compose logs -f          # watch boot
curl -fsS localhost:7860/api/status
```

Open `http://<server-ip>:7860` (log in with `CASTOR_PASSWORD`).

> **GHCR auth:** `ghcr.io/deepfounder-ai/castor` may be private. If the pull is
> denied, either make the package public in the GitHub UI
> (Packages → castor → Settings → Change visibility), or authenticate once:
> `echo $GITHUB_TOKEN | docker login ghcr.io -u <user> --password-stdin`.
> If you can't access the registry at all, use **build from source** below.

## Build from source (always works)

```bash
git clone https://github.com/deepfounder-ai/castor.git
cd castor
cp .env.example .env && nano .env
# edit docker-compose.yml: comment `image:`, uncomment `build: .`
docker compose up -d --build
```

---

## Updating

```bash
docker compose pull          # fetch the new image (prebuilt path)
docker compose up -d         # recreate the container
# build-from-source path: git pull && docker compose up -d --build
```

`./castor-data` is untouched — memory, threads, and settings carry over.

## Backups

```bash
# Cold backup (stop for a consistent SQLite snapshot)
docker compose stop
tar czf castor-backup-$(date +%F).tar.gz castor-data
docker compose start
```

Castor also writes periodic db snapshots to `castor-data/backups/`.

---

## Terminal access (CLI)

Castor has a full terminal chat mode (`castor` with no args) with slash commands
(`/help`, `/model`, `/provider`, `/soul`, `/skills`, `/thread`, `/memory`,
`/cron`, `/doctor`, …). It works great natively, but mixing it with the Docker
web container needs care.

> **One process per data dir.** Qdrant runs in **disk mode** and refuses a second
> opener of the `memory/` folder (`Storage folder is already accessed by another
> instance`). The running `--web` container already holds that lock, so you
> **cannot** run the CLI against the same `/data` at the same time.

Options:

- **In Docker → use the web UI.** Don't try to run the CLI alongside the live
  web server on the same volume.
- **Run the CLI in its own container, with the web stopped:**
  ```bash
  docker compose stop                     # release the Qdrant lock
  docker compose run --rm castor castor   # interactive terminal chat
  # ...exit, then bring the web server back:
  docker compose up -d
  ```
- **Quick one-off, non-interactive** (also needs web stopped):
  ```bash
  docker compose run --rm castor castor --doctor
  ```
- **Want both at once?** Run a native terminal install on a *different* machine
  or a *separate* `CASTOR_DATA_DIR`, or switch Qdrant to server mode
  (`CASTOR_QDRANT_MODE=server` + a Qdrant container) so multiple processes can
  share the vector store.

## Notes

- **Web auth:** set `CASTOR_PASSWORD` in `.env` whenever the port is reachable
  from anywhere but localhost. Empty password = no auth.
- **HTTPS / mic / camera:** browsers need HTTPS for mic & camera. Terminate TLS
  with a reverse proxy (Caddy/nginx/Traefik) in front of `:7860`, or run with
  `--ssl` (self-signed) by overriding the command.
- **Local LLM on the same host:** `host.docker.internal` only works on Docker
  Desktop. On a Linux server point `CASTOR_LLM_URL` at the docker bridge
  gateway, e.g. `http://172.17.0.1:1234/v1`, or run the LLM in its own
  container on the same compose network.
- **Browser tools:** the image bundles Chromium; `shm_size: 1gb` in the compose
  file prevents "Target closed" crashes. Headless login flows (OAuth/2FA) often
  fail by design — use a desktop install with a visible browser for those.
- **Resources:** ~2 GB image (Chromium + embedding model). Give the container
  ≥2 GB RAM; FastEmbed loads the embedding model on first memory use.
