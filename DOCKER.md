# Running the whole stack in Docker

One image (`iotdash:latest`) runs **web / worker / beat**, and (opt-in) the
**estreamer** ingester. Compose also runs **postgres**, **redis** and **nginx**.

```
nginx :80  ─►  web (gunicorn)  ─►  postgres        worker + beat ─► redis
                                     ▲                     │
             estreamer (eNcore ──JSON──► ingester) ────────┘  (FMC :8302)
```

## How code changes reach a running container — the key point

The image **bakes the code in** (`COPY . .`). A running container keeps the code
from when its image was built — editing files on the host or `git pull` does
**nothing** until you rebuild and recreate:

```bash
git pull
docker compose build            # bake new code into iotdash:latest
docker compose up -d            # recreate the changed containers
```

Because web/worker/beat share the one image, a single `build` updates all of
them and `up -d` recreates them. If you also run the estreamer profile, rebuild
its image too (it layers on `iotdash:latest`):

```bash
docker compose --profile estreamer build
docker compose --profile estreamer up -d
```

What each kind of change needs:

| You changed… | Do this |
|---|---|
| Python / templates / static | `docker compose build && docker compose up -d` |
| `requirements*.txt` | same (rebuild reinstalls deps) |
| DB models (a migration) | `docker compose run --rm web python manage.py migrate` (or just restart `web` — its entrypoint runs migrate when `RUN_INIT=1`) |
| `.env.prod` (env only) | `docker compose up -d` (recreate — **no rebuild**) |
| `docker/nginx.conf` | `docker compose restart nginx` |

### Fast dev loop (no rebuild per edit)
Add an override so the container runs your working tree live:
```yaml
# docker-compose.override.yml
services:
  web:
    volumes: [".:/app"]
    command: ["python","manage.py","runserver","0.0.0.0:8000"]
```
Then edits reflect on the next request. (Only `requirements*.txt` changes still
need a rebuild.) The override is auto-loaded by `docker compose up`; keep it out
of prod.

## First run

```bash
cp .env.prod.example .env.prod    # set DJANGO_SECRET_KEY, POSTGRES_PASSWORD,
                                  #   DJANGO_ALLOWED_HOSTS, ISE_*/FMC_*
docker compose build
docker compose up -d              # postgres, redis, web, worker, beat, nginx
docker compose logs -f web        # watch migrate + collectstatic + gunicorn
curl -sI http://localhost/        # expect 200/302
```

Notes:
- Compose overrides `POSTGRES_HOST=postgres` and `REDIS_URL=redis://redis:6379/0`
  so containers talk over the compose network, regardless of the `localhost`
  values in `.env.prod`.
- Only the **web** container runs migrate + collectstatic (`RUN_INIT=1`); worker
  and beat just wait for Postgres and start.
- Put your real hostname in `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS`.

## Seed the IoT inventory / run the API probe

```bash
docker compose run --rm web python manage.py sync_ise
docker compose run --rm web python manage.py probe_apis      # writes to the container; add -v for a mount
```

## eStreamer ingestion (opt-in profile)

Needs the FMC client cert and a config:
```bash
cp /path/to/client.pkcs12 ./client.pkcs12          # mounted read-only
# edit deploy/estreamer.conf.example: set subscription.servers[0].host = FMC IP
docker compose --profile estreamer build
docker compose --profile estreamer up -d estreamer
docker compose logs -f estreamer                    # eNcore handshake + "Ingested N"
```
The `estreamer` container runs `encore.sh foreground | estreamer_ingest --source
stdin` internally (the config keeps `logging.stdOut:false` so stdout is pure
JSON). `pyOpenSSL` in the image auto-converts the pkcs12 — no manual openssl step.

> Bookmarks reset if the container is recreated (no state volume), so eNcore
> resumes from `start:2`/now. Fine for steady-state; for exactly-once replay use
> the host/systemd deployment instead.

## Everyday commands

```bash
docker compose ps                       # status
docker compose logs -f worker           # a service's logs
docker compose restart web              # restart one service
docker compose down                     # stop all (keeps the pgdata volume)
docker compose down -v                  # stop all AND delete the DB volume (destructive)
docker compose exec web python manage.py shell
```

## TLS

Terminate TLS at nginx: mount your cert/key and add a `443` server block to
`docker/nginx.conf` (redirect 80→443), then `docker compose restart nginx`.

---
This Docker path and the systemd path ([SETUP.md](SETUP.md)) are alternatives —
run one or the other, not both, against the same database.
