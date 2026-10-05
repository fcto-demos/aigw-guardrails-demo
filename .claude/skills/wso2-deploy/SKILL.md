---
name: wso2-deploy
description: Deploy WSO2 API Platform components — AI Workspace, Platform API, API Portal, AI Gateway or API Gateway — onto a VM with Docker Compose. Use when asked to deploy, install, provision or stand up any of these components, whether onto a new AWS instance or an existing host. Handles hostname and TLS setup, control-plane and gateway configuration, and verification.
---

# Deploy WSO2 API Platform components

Stands up any combination of the components on one or more VMs using Docker
Compose, with Let's Encrypt TLS on a single hostname.

## Read this first

Load the reference files as you need them — they carry the failure modes, and
most of them are silent:

| File | When |
|---|---|
| `../_reference/infrastructure.md` | provisioning a new AWS VM |
| `../_reference/tls-and-certs.md` | always |
| `../_reference/control-plane.md` | deploying Workspace / Platform API / Portal |
| `../_reference/gateway.md` | deploying a gateway |
| `../_reference/operations.md` | verification and lifecycle |

## Step 1 — Establish what is wanted

Ask, and do not assume. Use one round of questions rather than drip-feeding.

**Which components?** Note that AI Workspace, Platform API and API Portal are
**profiles in a single Compose stack** — deploying two of them is one deployment,
not two. `platform-api` is mandatory for the other two. A gateway is a separate
stack and may live on the same host or a different one.

**Where?**
- a new AWS VM (you provision it), or
- an existing host they will give you SSH access to

**Hostname.** **One** hostname covers everything — ports distinguish the
components. Do not propose one hostname per component unless they want a reverse
proxy on 443. Confirm they can create a DNS A record for it, and in which zone.

**Which control plane** for a gateway: a self-hosted AI Workspace (this
deployment or another), or a SaaS one. This decides how the gateway reaches it
and whether port 9243 needs opening.

**Database.** SQLite is the default and is fine for a PoC — it survives restarts
and recreations in a named volume. Recommend PostgreSQL only if they need more
than one replica, point-in-time recovery, or survival of the instance itself.
Note the gateway's Postgres schema must be applied by hand.

**Distribution versions and where the zips come from** — a local copy, or the
GitHub releases. If a control plane is involved, check which gateway versions its
Get Started panel offers; deploying a gateway newer than the control plane
produces sync warnings.

## Step 2 — Provision, if new

Follow `infrastructure.md`. **Run the quota preflight before creating
anything** — a region can be over its Elastic IP limit because of other teams,
and finding out after provisioning a database means a rebuild.

Agree a resource name prefix and tags up front, so teardown can find everything
later.

Allocate the Elastic IP **early** and give the user the address immediately —
DNS propagation is usually the longest wait, and they can start it while you
continue.

## Step 3 — Prepare the host

Bootstrap per `infrastructure.md`: Docker CE plus the Compose plugin, `openssl`,
`unzip`, `jq`, `acl`, `certbot` with the DNS plugin, and `postgresql-client` only
if an external database is involved.

Unpack the distributions to `/opt/wso2/`.

## Step 4 — TLS

Verify DNS resolves **before** attempting issuance, and check the record itself
rather than trusting that it was created — the Route 53 console silently appends
the zone name to a full FQDN. See `tls-and-certs.md`.

Issue the certificate, then write and install the cert-deploy script. Get the
ownership and ACL handling right; a wrong mode leaves the listener unable to read
its own key, and the failure is at startup.

## Step 5 — Configure and start

Run each distribution's `./scripts/setup.sh` **before** overlaying config,
generating the admin password locally and passing it in. Save every credential
it prints — they are shown once.

**Back up the at-rest encryption keys immediately.** They have no recovery path.

Then apply the component config per `control-plane.md` and `gateway.md`. Both
files list the edits the shipped configs need — several are additions of tables
that are absent, and one template block is actively wrong for this stack.

Keep secrets in `api-platform.env` as `{{ env }}` references so `config.toml`
stays safe to commit.

Start with `docker compose up -d`, never `restart`.

## Step 6 — Verify, don't assume

Check each of these and report actual output:

- every health endpoint returns OK
- containers are `healthy`, not merely `Up`
- environment values actually reached the containers
  (`docker compose exec <svc> env | grep APIP_`)
- external TLS validates **without `-k`**
- ports that should be closed are unreachable from outside
- a gateway shows `Control plane connection established` and moves to **Active**
- for Postgres, the storage line names the external database

Then log in through a browser and confirm no certificate warning.

## Step 7 — Hand over

Record and give the user:

- every URL, including the Portal's full path with its organization handle
- credentials, and where they are stored
- what is theirs to do: DNS records, gateway registration in the control plane,
  LLM provider credentials
- the teardown list, if you provisioned infrastructure
- running cost, and that stopping the instance preserves everything

## Not covered — say so rather than improvising

One thing is deliberately out of scope. If asked for it, explain the gap and
what it would take, rather than half-implementing it.

**Identity providers for the portals.** Moving AI Workspace or the API Portal off
file-based authentication to OIDC (Asgardeo, Entra ID). Three complications worth
stating to the user:

- Setting `[platform_api.auth] mode = "idp"` **retires the file-based login
  endpoint**, so it must come after anything depending on it — notably the
  Portal's `seed-samples.sh`, which authenticates against it.
- Redirect URLs embed the hostname and port and are expensive to change once
  registered with the provider. The product docs also disagree about whether the
  AI Workspace callback carries the `/ai-workspace` path prefix — register both
  forms if attempting it.
- For the Portal, the docs disagree about whether the organization claim is
  `org_name` (display name) or `org_handle` (slug). Making both values identical
  in the IdP sidesteps it.

## Things that will bite you

Each of these cost real debugging time. They are detailed in the reference files;
this is the index.

**Silent failures**
- `restart` does not apply config or image changes — no error
- Editing or deleting `.env` orphans the data volumes — no error
- A Portal organization handle mismatch reports as "Invalid username or password"
- The Portal's shipped healthcheck calls `curl`, which is not in that image
- A gateway `404` may be a relayed *upstream* 404, not a routing failure

**Absent or wrong in shipped config**
- `[platform_api.server.https]` is missing entirely
- `[api_portal.*]` is missing, and the template's HTTPS block has paths that do
  not exist here
- `[controller.storage.postgres]` is missing
- Gateway analytics ships enabled with a placeholder ID — errors every request
- Gateway `insecure_skip_verify` ships `true`
- Gateway resource limits are quickstart-sized
- `[[policy_configurations.*.keymanagers]]` — absent; see `gateway.md`
- Fluent Bit ships with `trace_output On`, which makes it tail its own output

**Immutable or single-use**
- The Portal organization handle, after first start
- Gateway registration tokens
- `gateway_id` must be unique across the estate

**Environment quirks**
- zsh does not word-split unquoted variables in `aws` commands
- AWS CLI booleans are flags (`--no-deletion-protection`), not values
- Docker's GPG key must be stored armored on Ubuntu 24.04
- Containers run as UID 10001, which has no host account
- AWS does not hairpin an instance to its own Elastic IP
