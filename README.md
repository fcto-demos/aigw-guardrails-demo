# WSO2 AI Platform — Demo

A self-contained, local demo of the WSO2 AI Platform's gateway: LLM
showcases, each driving a real policy (routing, caching, guardrails, spend
limits, failover...) against Anthropic, Mistral and Gemini, and MCP showcases
where an agent uses tools through the gateway. Everything runs on
your machine with Docker Compose — nothing to deploy, nothing shared.

## Overview

Three components, wired together:

- **ai-chatbot** — the demo UI. A small chat app with one card per showcase,
  on two pages: **LLM Gateway** and **MCP Gateway**.
- **ai-gateway** — the WSO2 AI Gateway. Every request is policy-enforced
  before it reaches an LLM provider or an MCP server.
- **ai-workspace** — the control plane. Registers the gateway and configures
  its providers and policies.

| Showcase | What it shows |
|---|---|
| Just chat | The assistant with no demo scaffolding |
| Semantic routing | The gateway reads what the question means and picks the model |
| Time-based routing | During peak hours, even complex questions go to the fast model |
| Cost-based routing | As spend rises, the gateway moves traffic to the cheaper model |
| Model failover | A model goes down; the next request is answered by another one |
| Intelligent routing | An LLM reads each question and picks the provider and model |
| Semantic caching | A reworded question is answered from cache — no model call, no tokens |
| Prompt injection guardrails | The request is refused before any model is called: a pattern catches the obvious attempts, a meaning-based guard catches rewordings and other languages |
| PII masking | Personal data is swapped out before the request leaves the gateway |
| Content safety · Azure | Azure Content Safety checks the message and the model's answer: a threat of violence is refused, an angry customer is not |
| Content safety · AWS Bedrock | The same, through an AWS Bedrock guardrail |
| LLM spend limit | The cost budget refuses traffic once the money is gone |

On the **MCP Gateway** page, the assistant becomes an agent. Its tools come
from a small Acme Mobile MCP server (`mcp-servers/acme-mobile/`): list the
plans, read the customer's account, change the plan. Its model goes through the
LLM Gateway and its tool calls through MCP proxies; each call shows in the
reply, with what the gateway did.

| MCP showcase | What it shows |
|---|---|
| Agent with MCP tools | The agent reads and changes the account; no rule applied |
| Tool access control | The gateway hides `change_plan`: the agent can't call what it can't see |
| Tool argument check | The gateway checks each `change_plan` call's arguments: switching to Plus goes through, to Unlimited is refused before the server sees it |
| Tool rate limit | The gateway allows 3 `change_plan` calls per hour and per conversation; the 4th gets 429 |
| Authentication | Without a signed-in user the gateway answers 401; the page signs you in, as any MCP client would |
| Authorization | Same token, per-tool rule: only a user whose role grants `acme:plan:write` may change the plan |

The last two need an OpenID Connect identity provider (Asgardeo, WSO2 Identity
Server or any other): see [.docs/mcp-identity-provider.md](.docs/mcp-identity-provider.md).

Before a demo, `./scripts/reset-mcp.sh` puts the account back on the Everyday
plan.

## Preview

![Preview](.docs/preview.gif)

## Architecture

![Architecture diagram](.docs/architecture.svg)

Every showcase is its own lane: one gateway proxy per (showcase, provider),
each with the policy it demonstrates already attached. `ai-chatbot` never
configures the gateway at request time — it only picks which lane to call.

## Project structure

```
wso2-api-platform-ai-demo/
├── ai-chatbot/       demo UI + every showcase (showcases.json)
├── ai-gateway/       WSO2 AI Gateway — policies, custom image, compose stack
├── ai-workspace/     AI Workspace: AI control plane
├── mcp-servers/      MCP servers behind the MCP showcases — own compose stack
├── scripts/          setup, run, stop, logs, certificate generation
├── secrets/          API keys
├── .docs/            architecture diagram and other reference docs
├── demo.env          single source of truth: identity, versions, ports, analytics mode
└── providers.json    the LLM providers wired into every showcase
```

## Prerequisites

- **Docker**, with Compose v2 (`docker compose version`)
- **Python 3**
- **openssl**, **curl**, **unzip**, **htpasswd**
- At least one LLM provider API key: **Anthropic**, **Mistral** or **Gemini**
- Semantic caching needs a **Mistral** key specifically for embeddings —
  it's the only embeddings provider supported today. The same key you use
  for Mistral chat works fine for this too.

This demo has been tested most thoroughly with **Anthropic** and
**Mistral** — that's the combination we recommend. **Gemini**'s free tier
is more restrictive: some models aren't available on a newly created key,
and you're more likely to hit rate limits.

Intelligent routing spans two providers: it needs both an **Anthropic** and
a **Mistral** key, and is skipped when either is disabled in
`providers.json`.

Optional, for the Content safety cards (values in `secrets/content-safety.env`;
a card left out shows "No provider wired"):

- **Content safety · Azure (Tested)**: an [Azure AI Content Safety resource](https://aka.ms/acs-create),
  its endpoint and key
- **Content safety · AWS Bedrock (Not Tested yet)**: a [Bedrock guardrail](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-components.html)
  and a [Bedrock API key](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)

## Installation

### First run

`./scripts/reset-and-setup.sh` must be the first thing you run. It installs
the `ap` CLI, builds the custom gateway image, generates local certificates,
starts every container, and provisions every showcase — nothing else
here creates any of that.

```bash
cp secrets/api-keys.env.example secrets/api-keys.env
# edit secrets/api-keys.env — paste in at least one provider key

cp secrets/idp.env.example secrets/idp.env
# edit secrets/idp.env — to configure the AuthN / AuthZ use cases (MCP)

cp secrets/content-safety.env.example secrets/content-safety.env
# edit secrets/content-safety.env — Azure Content Safety and/or AWS Bedrock Guardrails (optional, Content safety cards)

cp ai-workspace/api-platform.env.example ai-workspace/api-platform.env
cp ai-gateway/api-platform.env.example ai-gateway/api-platform.env

./scripts/reset-and-setup.sh
```

The two `api-platform.env` files above need no editing for a local run —
every value in them is either already correct as shipped, or filled in
automatically on first run: the admin password hash (by
`generate-local-certs.sh`) and the gateway's control-plane registration
token (by `provision_showcases.py`). Setting these up by hand, without
`reset-and-setup.sh`, isn't documented here.

First run takes a few minutes.

### After that

```bash
./scripts/run-local.sh      # start again — keeps existing data
./scripts/stop-local.sh     # stop everything
./scripts/logs-local.sh     # tail every component's logs together
./scripts/reset-mcp.sh      # MCP demo account back on the Everyday plan

# Content safety, once secrets/content-safety.env is filled in after the first run
./scripts/python/provision_showcases.py content-safety mistral-content-safety gemini-content-safety
./scripts/python/provision_showcases.py content-safety-bedrock mistral-content-safety-bedrock gemini-content-safety-bedrock
```

Only reach for `reset-and-setup.sh` again if you actually want a clean
slate — it wipes every database (providers, gateway registrations, cached
responses) and provisions from scratch.

### Try it out

Connect to the ai-chatbot URL and try the different showcases.

After that, connect to AI Workspace to see how it's all modeled in the platform:

The gateway, registered and active:
![Gateway](.docs/preview-ai-workspace-ai-gateway.png)

One LLM Provider per provider:
![LLMProvider](.docs/preview-ai-workspace-llm-provider.png)

One LLM Proxy per showcase / guardrail:
![LLMProxy](.docs/preview-ai-workspace-llm-proxy.png)

One MCP proxy per showcase:
![MCPProxy](.docs/preview-ai-workspace-mcp-proxy.png)

One Application per provider (for analytics purposes):
![Apps](.docs/preview-ai-workspace-apps.png)

## Configuration

`demo.env`, at the repo root, is the single source of truth shared by every
component: the demo's name, its hostname (for a VM deployment — local dev
never reads it), component versions, every port except ai-gateway's own
(set directly in `ai-gateway/.env`), and `ANALYTICS_MODE` (`moesif` or
`local` — see the architecture diagram above).

The local dev scripts source it automatically; nothing needs editing for a
first run on default ports.

To provision the showcases onto an **already-running** AI Workspace and
gateway instead of this repo's own stack, set `AIW_URL`, `GATEWAY_ID`,
`GATEWAY_ENDPOINT`, `ADMIN_USERNAME`/`ADMIN_PASSWORD` (and optionally
`DEMO_PROJECT_ID`), then run `scripts/python/provision_showcases.py --check`
(read-only: lists what would be created or overwritten) before
`--existing-gateway`. Point `ai-chatbot/config.env`'s `GATEWAY_BASE` at that
gateway, and in `providers.json` disable any provider whose id already
exists there.

## Troubleshooting

- **"docker is not running"** — start Docker Desktop (or Colima) first.
- **"missing secrets/api-keys.env"** — `reset-and-setup.sh` refuses to
  continue without it; the error message shows the exact copy-paste fix.
- **A showcase's card loads but refuses to chat** — that provider has no key
  set for that lane. Expected for any provider you left blank; check
  `secrets/api-keys.env` for the one you expected to work.
- **Intelligent routing fails to deploy after a `git pull`** — the custom
  gateway image is only built when missing. Remove it
  (`docker image rm ghcr.io/wso2/api-platform/ai-demo-gateway-runtime:1.2.0`)
  and re-run `reset-and-setup.sh` so it picks up the new policy.
- **A fresh restart briefly 502s** — recreating `gateway-controller` and
  `gateway-runtime` together causes a short xDS reconnect race. It is
  benign and clears up within 10–20 seconds.
- **Nothing else works** — `./scripts/stop-local.sh` then
  `./scripts/reset-and-setup.sh` gets you back to a known-good state.

