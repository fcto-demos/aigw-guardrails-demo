---
name: wso2-custom-policy
description: Add a custom policy to a WSO2 AI Gateway or API Gateway by rebuilding its container image with the ap CLI, then rolling the new image out. Use when asked to add, install or deploy a custom policy, rebuild the gateway image, or update the policy set — including when a colleague has written a policy and pushed it to a repository.
---

# Add a custom policy to a gateway

Policies are **compiled into the gateway image**, not loaded at runtime. Adding
one means producing a new image. There is no way to drop a policy onto a running
gateway.

Read `../_reference/gateway.md` for the gateway's config and verification, and
`../_reference/operations.md` for the rollout rules.

## What is not affected

**Deployed artifacts survive untouched.** APIs, LLM providers, proxies,
subscriptions and keys live in the controller's database — SQLite in a named
volume, or an external Postgres. An image swap replaces containers and reattaches
the same storage, and **the gateway keeps its registration**, so no new
registration token is needed.

## Prerequisites

The `ap` CLI, on a machine with Docker. **Build on the gateway VM if you can** —
the image then exists where it runs, with no registry and no transfer.

```bash
curl -sfLO https://github.com/wso2/api-platform/releases/download/ap%2Fv<VER>/ap-linux-amd64-v<VER>.zip
unzip -q ap-linux-amd64-v<VER>.zip
sudo install -m 755 ap-linux-amd64-v<VER>/ap /usr/local/bin/ap
ap version
```

Two things the published install steps get wrong:

- **The binary is nested in a directory inside the zip**, not at its root, so a
  bare `mv ap ~/bin/` fails.
- It is **`ap version`**, not `ap --version` — the latter errors as an unknown
  flag.

## Step 1 — Get the policy onto the build machine

```bash
cd /opt/wso2/wso2apip-ai-gateway-<ver>
mkdir -p policies
git clone <repo-url> policies/<name>
```

The policy directory needs `go.mod`, the Go source, and
`policy-definition.yaml` at its top level. If the repo holds several policies,
point at the subdirectory.

If they copied a WSO2 sample, check that **`go.mod`'s module path was renamed** —
a half-renamed copy still declaring the original policy's path is the most common
authoring mistake and produces confusing build errors.

## Step 2 — Register it in `build.yaml`

Two source types, and picking the wrong one is the usual stumble:

| Field | For | Value |
|---|---|---|
| `filePath` | **a local policy under development** | path **relative to `build.yaml`**, not to where you run the command |
| `gomodule` | policy-hub policies, and your own once published | `github.com/org/repo/policies/name@v1` |

```yaml
policies:
  # ... existing entries, leave them alone ...
  - name: <name>
    filePath: ./policies/<name>
```

## Step 3 — Build

```bash
ap gateway image build --name <short-name>
```

Run it from the directory containing `build.yaml`. **There is no `--config`
flag** — some documentation shows one; the shipped README is wrong on this.
`--name` defaults to the directory name and `--repository` defaults to
`ghcr.io/wso2/api-platform`, which is where the image *name* comes from. Check
`ap gateway image build --help` if the installed version differs.

It prints two image names. **Copy them exactly** — do not construct them:

```
✓ Built gateway images with 1 policies:
  • ghcr.io/wso2/api-platform/<name>-gateway-runtime:<ver>
  • ghcr.io/wso2/api-platform/<name>-gateway-controller:<ver>
```

**Check the policy count.** It should equal the number of local policies added.
`0 policies` means the `filePath` is wrong — **the build still succeeds** and
hands you a working image without the policy in it. That is the failure most
likely to waste an afternoon.

The count refers only to *local* policies. The 54 shipped ones come from the base
image, so a build with none added still yields an image carrying all of them.

`build-manifest.yaml` is rewritten by each build. Diff it against the previous
version to confirm only the intended change.

### The image name is not a location

The `ghcr.io/...` prefix is only a label. The images exist **solely in the local
Docker store of the machine that built them** — nothing is uploaded unless you
pass `--push`. The build says so: *"Images available in local Docker"*.

Consequence: **`docker compose pull` will now fail**, because it tries to fetch
that name from a registry where it does not exist. `docker compose up -d` is
fine — it uses a local image when present.

To move an image to another host, either push it to a registry you control and
reference *that* name, or stream it:

```bash
docker save <controller-image> <runtime-image> \
  | gzip | ssh -i <key> ubuntu@<host> 'gunzip | docker load'
```

Roughly 600 MB for the runtime and 210 MB for the controller.

### Base image overrides

The build resolves three images: the **builder**, plus the **controller** and
**runtime** bases it extends. All default to public GHCR at `gateway.version`.
Override under `gateway.images` for an internal mirror, an air-gapped
environment, or a WSO2 subscription — the latter **requires all three** pointed
at `registry.wso2.com/wso2-api-platform/` with four-digit U2 versions
(`1.2.0.0`), after `docker login registry.wso2.com`.

## Step 4 — Roll it out

Put **both** names in `docker-compose.override.yaml`:

```yaml
services:
  gateway-controller:
    image: ghcr.io/wso2/api-platform/<name>-gateway-controller:<ver>
  gateway-runtime:
    image: ghcr.io/wso2/api-platform/<name>-gateway-runtime:<ver>
```

Note the previous tags first, so rollback is trivial, and do not
`docker image prune` them away.

```bash
docker compose up -d
```

**Two ways this goes wrong:**

- **`docker compose restart` will not pick up the new image.** It reuses the
  existing container, the policy silently does not appear, and nothing errors.
- **Swap both images together.** The policy set is compiled into each: the
  controller validates and deploys the policy, the runtime executes it. A
  mismatched pair gives a controller that accepts a policy the runtime cannot run.

## Step 5 — Verify

```bash
# the definition is in the image — CUSTOM policies land in /app/policies,
# versioned as <name>-v<ver>.yaml. /app/default-policies holds the shipped ones.
docker compose exec gateway-controller ls /app/policies | grep <name>

# and confirm it was actually linked into the runtime binary
docker compose exec gateway-runtime grep -ac <name> /app/policy-engine

# the manifest pushed to the control plane — policy_count should have gone up
docker compose logs gateway-controller 2>&1 | grep -i 'gateway manifest'

# nothing broke
docker compose logs gateway-controller --since 5m 2>&1 | grep -i 'level=ERROR'

# still serving — from the HOST; the runtime image has no curl or wget
curl -fsk https://localhost:8443/_gateway-health/ready
```

Then attach the policy to an API or proxy in the control plane — it appears in
the picker with the parameters from its `policy-definition.yaml`.

## Rollback

Restore the previous `image:` values and `docker compose up -d`. Nothing is lost:
storage reattaches and the registration survives.

## Writing a policy, if they have not yet

Each policy is its own Go module:

```
policies/<name>/
├── go.mod
├── <name>.go
└── policy-definition.yaml
```

It implements only the phases it needs, against
`github.com/wso2/api-platform/sdk/core/policy/v1alpha2`:

```
Request Headers   → OnRequestHeaders()
Request Body      → OnRequestBody()      or OnRequestBodyChunk() to stream
Response Headers  → OnResponseHeaders()
Response Body     → OnResponseBody()     or OnResponseBodyChunk()
```

`policy-definition.yaml` declares `name`, `version` and the `parameters` the
control-plane UI renders. Mark rarely-changed parameters
`x-wso2-policy-advanced-param: true` to tuck them under "Advanced".

Body-inspecting policies buffer the full payload, which is why guardrails cost
noticeably more CPU on LLM traffic than on REST.

Start from [WSO2's sample policies](https://github.com/wso2/api-platform/tree/main/gateway/sample-policies)
rather than an empty module — and rename `go.mod`'s module path.
