# Operations — applying changes safely

## The lifecycle rule that matters most

```bash
docker compose up -d                    # applies config and image changes
docker compose up -d --force-recreate   # forces new containers (needed after a cert change)
docker compose restart <svc>            # process bounce ONLY
```

**`restart` does not pick up changes.** It reuses the existing container, so a
modified `config.toml` or a changed `image:` tag is silently ignored — no error,
no warning, and the old behaviour persists. This is the single most common way to
"apply" a change and have nothing happen.

Config files are bind-mounted, so Compose cannot detect that their *contents*
changed. `up -d` alone may not recreate a container whose only change is inside a
mounted file — use `--force-recreate` when you need certainty, and always after
replacing certificates.

`restart` is fine for one thing: forcing an immediate retry of a failing
connection without changing anything.

## None of that loses data

`up -d`, `--force-recreate`, `down` then `up -d` all replace *containers* and
reattach the *same* named volumes. Container filesystems are disposable; named
volumes have their own lifecycle.

| Action | Data |
|---|---|
| `restart` / `up -d` / `--force-recreate` / `down` then `up -d` | safe |
| `down -v` | **destroys** |
| `docker volume rm <name>` | **destroys** |
| `docker system prune --volumes` | **destroys** |
| editing or deleting `COMPOSE_PROJECT_NAME` in `.env` | **effectively destroys** |

That last one is the dangerous one because **nothing warns you**: volume names
are prefixed with the project name, so a different value silently brings the
stack up against fresh empty volumes. Never edit or delete `.env`. A re-unpacked
distribution generates a *new* project name and behaves the same way — build in a
fresh directory if you like, but run `docker compose` from the original.

```bash
docker volume ls --filter name=wso2apip
```

**`docker compose --profile <name> down` tears down the whole project**, not just
that profile. To stop one optional service use `docker compose stop <service>`.

## Applying a config change

1. Edit `configs/config.toml` (or append to `api-platform.env`)
2. `docker compose config --quiet` — catches syntax errors before restarting
3. `docker compose up -d`
4. Verify the value actually reached the container:
   ```bash
   docker compose exec <svc> env | grep '^APIP_'
   ```
5. Check the logs for a startup failure — these services **fail closed**, naming
   the key or file path that did not resolve

For a config change tracked in this repo, edit the repo copy and copy it up, so
the two do not drift.

## What a recreate costs

**AI Workspace sessions are in memory** — the session store supports only
`memory` in this version. Any recreation of `ai-workspace` signs out every
logged-in user, regardless of database. Warn before doing it during a demo.

Gateways keep serving traffic through a controller restart; Envoy holds its
configuration.

## After certificate renewal

Neither the control-plane services nor Envoy reload certificates while running,
and neither watches the file. Run the cert-deploy script, which places the files
*and* recreates the containers. See `tls-and-certs.md`.

## Health checks

```bash
curl -fsk https://localhost:9243/health                    # platform-api
curl -fsk https://localhost:9643/healthz                   # ai-workspace
curl -fsk https://localhost:9543/health                    # api-portal
curl -fsk https://localhost:8443/_gateway-health/ready     # gateway (Envoy)
curl -s   http://localhost:9094/api/admin/v1/health        # gateway controller
```

**`localhost` is correct** — these run on the host against published ports, and
anything running *inside* a container also uses `localhost`. Only
service-to-service hops use the public hostname.

Probe the gateway runtime and the API Portal **from the host**: the runtime image
has no `curl`, the Portal image has no `wget`.

## Logs

```bash
docker compose logs -f <service>
docker compose logs --tail 100 <service>
docker compose logs --since 10m
docker compose logs <service> 2>&1 | grep -iE 'level=(ERROR|WARN)'
```

| Service | Look here for |
|---|---|
| `ai-workspace` | sign-in, sessions, proxied calls. A `502` mentioning certificate verification means the BFF→Platform API hop broke |
| `api-portal` | one `[AUDIT]` line per request; `organization_mismatch` on login failures |
| `platform-api` | control-plane API, auth, database |
| `gateway-controller` | registration, control-plane sync, deployments, storage |
| `gateway-runtime` | `[rtr]` Envoy access log, `[pol]` policy engine |

`docker compose` must run **from inside** the relevant directory — that is what
picks up `.env` and the override file. Running it elsewhere silently targets a
different, empty project.

## Instance lifecycle

```bash
aws ec2 stop-instances  --instance-ids <id>
aws ec2 start-instances --instance-ids <id>
```

An Elastic IP survives stop/start, so DNS keeps working and nothing needs
reconfiguring. Stopping removes almost all the cost while preserving volumes,
keys and certificates — worth suggesting for an environment used only for demos.

## When SSH fails

`i/o timeout` means dropped packets — a firewall, almost always a security-group
rule scoped to an IP that has since changed. `Permission denied (publickey)`
means the key: wrong file, or permissions too open (`chmod 400`).

The web endpoints are independent of SSH, so check those first to establish
whether the host is actually down.
