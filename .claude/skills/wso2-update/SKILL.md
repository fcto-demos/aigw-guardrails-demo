---
name: wso2-update
description: Apply a change to a running WSO2 API Platform deployment — edit configuration, renew or replace TLS certificates, change ports or hostnames, enable analytics or observability, restart services safely, or diagnose why a change did not take effect. Use when asked to update, change, reconfigure, restart, renew certs for, or troubleshoot an already-deployed AI Workspace, API Portal, Platform API or gateway.
---

# Update a running WSO2 deployment

For a deployment that already exists. Read `../_reference/operations.md` first —
it carries the lifecycle rules. Then whichever of `control-plane.md`,
`gateway.md` or `tls-and-certs.md` is relevant.

## The rule behind almost every "it didn't work"

**`docker compose restart` does not pick up changes.** It reuses the existing
container, so a modified `config.toml` or a changed `image:` tag is silently
ignored — no error, and the old behaviour persists.

Use `docker compose up -d`, and `--force-recreate` when you need certainty. Since
config files are bind-mounted, Compose cannot see that their *contents* changed,
so `up -d` alone occasionally will not recreate — `--force-recreate` always will.

**None of this loses data.** Named volumes reattach. Only `down -v`,
`docker volume rm`, `docker system prune --volumes`, or changing
`COMPOSE_PROJECT_NAME` destroy anything.

## Procedure

**1. Find out what is actually deployed.** Do not work from assumptions or from
what a document claims:

```bash
cd /opt/wso2/<distribution>
docker compose ps
grep -v '^#' .env | grep -v '^$'
docker compose exec <svc> env | grep '^APIP_'
```

**2. Make the change in the right place.** Secrets and per-environment values
belong in `api-platform.env` as `{{ env }}` references; structural settings in
`configs/config.toml`; container-level changes in
`docker-compose.override.yaml`, never the shipped `docker-compose.yaml`.

If the deployment is tracked in this repo, edit the repo copy and copy it up, so
the two do not drift.

**3. Validate before restarting.**

```bash
docker compose config --quiet
```

**4. Apply.** `docker compose up -d` — and warn first if `ai-workspace` is
included, because **its sessions are in memory and every logged-in user is
signed out**.

**5. Verify the change actually landed**, in the container rather than the file:

```bash
docker compose exec <svc> env | grep <VAR>
docker compose logs <svc> --since 3m 2>&1 | grep -iE 'level=(ERROR|WARN)'
```

These services fail closed and name the key or file path that did not resolve, so
read the startup log rather than guessing.

## Common changes

### Certificate renewal

Neither the control-plane services nor Envoy reload certificates while running,
and neither watches the file. **An expired certificate is an outage**, not a
browser warning, because internal hops verify certificates too.

```bash
sudo certbot renew --force-renewal
sudo /opt/wso2/deploy-certs-remote.sh          # places the files AND recreates
```

If renewal automation was skipped, note that `certbot.timer` still renews — it is
only the deploy hook that is missing, so installing it is nearly free.

### Enabling analytics

Set `[analytics] enabled = true`, add `enabled_publishers`, and put the
application ID in `api-platform.env` — never as a literal in the TOML. Confirm
`Moesif publisher added` appears in the runtime log and no publish errors follow.
The shipped default has a **placeholder** ID that errors on every request, which
is why it is normally disabled.

### Changing a hostname or port

Touches more than it looks: `APIP_AIW_DOMAIN`, both internal-hop URLs, the
Portal's `base_url`, the gateway's `controlplane_host`, the `extra_hosts`
entries, the certificate SAN, the DNS record, and the security group. Work
through `tls-and-certs.md` and `control-plane.md` rather than grepping for the
old value.

Health checks and probes stay on `localhost` — rewriting those breaks
`depends_on: service_healthy`.

### Observability profiles

```bash
docker compose --profile metrics up -d      # Prometheus 9092, Grafana 3000
docker compose --profile logging up -d      # OpenSearch 9200, Dashboards 5601
docker compose --profile tracing up -d      # Jaeger 16686
```

Those ports should stay closed; reach them over an SSH tunnel.

**Fix the shipped configs before starting the logging profile.** Fluent Bit
ships `trace_output On`, which makes it log every record it ships into its own
stdout — which it is tailing. That loop measured ~700,000 docs/minute here. The
OTel collector's `debug` exporter at `verbosity: detailed` feeds it further. See
`../_reference/gateway.md` for the three edits; do them first, not after.

**Tracing needs `[tracing]` in `config.toml` and a forced recreate** — starting
the profile alone leaves the gateway on its old config, silently. Use
`docker compose up -d --force-recreate gateway-controller gateway-runtime`.

**`docker compose --profile <name> down` tears down the entire project**, gateway
included. Use `docker compose stop <service>`.

**Name the services on any recreate** when other people's containers share the
stack — a bare `up -d --force-recreate` restarts everything in the project.

### A custom policy

Use the `wso2-custom-policy` skill — it is an image rebuild, not a config change.

## Diagnosing

Read the logs before theorising, and let the error text narrow it:

| Symptom | Likely cause |
|---|---|
| Change had no effect | `restart` was used instead of `up -d` |
| Stack came up empty | `COMPOSE_PROJECT_NAME` changed, or `.env` lost |
| `502` mentioning certificate verification | an internal hop; check `extra_hosts` resolves |
| Portal rejects correct credentials | organization mismatch — grep the Portal log for `organization_mismatch` |
| Container permanently `(unhealthy)` but serving | healthcheck references a binary absent from the image |
| Gateway `404` | read Envoy's route config; a JSON 404 body is usually a relayed upstream 404 |
| Gateway stuck `Not Active` | reachability, then the token (single-use), then certificate trust |
| SSH `i/o timeout` | security group; `Permission denied (publickey)` is the key |
| Log index growing by GB/hour | Fluent Bit `trace_output On` tailing its own log |
| Tracing enabled but Jaeger empty | gateway not recreated; bind-mounted config unchanged to Compose |
| Policy config key has no effect | the engine ignores unknown keys silently — check it exists |

**Correct the record when a hypothesis turns out wrong.** Say so plainly and move
to the next one rather than layering guesses.

## Keep the repo in step

After any change on the host, copy the changed file back into this repo so it
stays an accurate record. Never copy `api-platform.env`, `resources/keys/`, or
anything under `resources/certificates/` or `resources/listener-certs/` — the
`.gitignore` excludes them, and the repo's value depends on that staying true.
