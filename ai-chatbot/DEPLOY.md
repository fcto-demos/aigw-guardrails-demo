# Deploying the AI chatbot

The demo front end for the PoC: a branded assistant that reaches Anthropic
**only** through the AI Gateway, plus the nine-showcase harness that makes each
policy's effect visible. [`README.md`](README.md) documents what the app *is* and
how each showcase behaves. This file is only about getting it running.

Nothing here configures the gateway. The app assumes the lanes already exist —
see [What the gateway must already have](#what-the-gateway-must-already-have).

## Prerequisites

Python 3, standard library only. No dependencies, no bundler, no build step —
`static/` is ES modules the browser loads directly. Verified on Python 3.9.6;
anything newer is fine.

That is the entire list. There is no `requirements.txt` because there is nothing
to install.

You do need **nine `api-key-auth` keys**, one per LLM provider — see
[Credentials](#credentials). Without them the gateway refuses every request 401.

## Run it locally

```bash
cd ai-chatbot
python3 server.py
```

The tracked `config.env` already wires all nine lanes, but its API keys are
blank, so on a fresh checkout every request is refused `401 Valid API key
required`. Paste the nine keys in first — see [Credentials](#credentials).

Open <http://localhost:8600>. The startup banner is the deployment check. All
nine lanes resolved and keyed:

```
  key      gateway-side key on 9/9 showcases, header x-api-key

  showcases
    ok      semantic-routing     /anthropic-semantic-routing showcases.json contextSuffix
    ok      time-based-routing   /anthropic-time-routing  showcases.json contextSuffix
    ok      cost-based-routing   /anthropic-cost-routing  showcases.json contextSuffix  (no streaming)
    ok      model-failover       /anthropic-failover      showcases.json contextSuffix
    ok      semantic-caching     /anthropic-cache         showcases.json contextSuffix  (no streaming)
    ok      prompt-injection     /anthropic-guardrail     showcases.json contextSuffix
    ok      pii-masking          /anthropic-pii-masking   showcases.json contextSuffix  (no streaming)
    ok      spend-limit          /anthropic-cost-limit    showcases.json contextSuffix  (no streaming)
    ok      plain-chat           /anthropic-plain         showcases.json contextSuffix

  9 of 9 wired.
```

`key` reading `none sent` or `key on 3/9` is the fastest tell that a key is
missing — the banner says so before any request 401s.

A lane printed as `MISSING` or `unwired` loads its card and **refuses to chat**,
deliberately: falling back to the plain provider would let a card answer normally
with no policy attached, and an audience would conclude the capability works when
it was never there.

`curl -s localhost:8600/health` reports the same resolved configuration as JSON,
without making a model call.

## Configuration

Precedence, lowest to highest:

1. built-in defaults in `server.py` — already the hosted PoC gateway
2. `config.env` in this directory
3. real environment variables
4. `--port`, `--model`, `--gateway-base`, `--config`

`config.env` is tracked here and holds the PoC's live lane wiring — with the
nine `SHOWCASE_*_API_KEY` values blank, because those are the only secrets in
it. `config.env.example` is the annotated template, documenting every key at
length.

The settings that matter:

| Key | Why it is set the way it is |
|---|---|
| `GATEWAY_BASE` | scheme://host:port every showcase's computed context hangs off. `--gateway-base` moves **every** showcase at once. |
| `SHOWCASE_*_CONTEXT` | One context path per showcase. This is the whole wiring: `GATEWAY_BASE` + context + `/v1/messages`. |
| *(no `MODEL=`)* | No global default to set: each request's default is that provider's own fast/cheap model from `providers.json`, resolved per-showcase — the gateway's routers choose the model that actually answers regardless. `--model <id>` overrides every showcase at once with a literal id of your own, useful for demoing the error path. |
| `MAX_TOKENS=2048` | Keep it generous. A reasoning model spends output tokens on thinking *before* it writes; at 32 this gateway returned a `thinking` block and no text — a blank reply, not an error. |
| `BRAND_NAME` | Rebrands the whole app: wordmark, page title, system prompt. |
| `SHOWCASE_PROMPT_INJECTION_SYSTEM_PROMPT` | Deliberately permissive. The stock prompt already refuses to disclose itself, so a refusal would prove nothing about the guardrail. |
| `POLICY_BLOCK_MARKERS` | `GUARDRAIL_INTERVENED,REGEX_GUARDRAIL` — lets the UI paint a policy refusal as the success it is rather than as a fault. |
| `GATEWAY_API_KEY_HEADER=x-api-key` | Must match the `api-key-auth` policy's `key` parameter (`provision_showcases.py` provisions it as `X-API-Key`; header names are case-insensitive, and this gateway has always been driven with the lowercase spelling, verified against the real thing). Coincidentally the same name as the header the gateway itself uses toward Anthropic, on a different hop — no collision, just don't let the resemblance confuse a future rename. |
| `SHOWCASE_*_API_KEY` | One per lane, nine different values. The only secrets in the file, and **blank in git** — fill them in locally. |
| `GATEWAY_API_KEY` | Fallback used by any lane with no key of its own. Leave empty when all nine are keyed. |

### `TLS_VERIFY` can be `true` now

The tracked value is `false`, from when the host's certificate was untrusted.
The host now presents a valid Let's Encrypt certificate for
`<DEMO_HOSTNAME>` (issuer `Let's Encrypt YE1`, expiring
**25 Nov 2026**), and a full streamed turn was verified end-to-end with
`TLS_VERIFY=true`. The server prints a warning at startup while verification is
off, so leave it off only knowingly:

```bash
TLS_VERIFY=true python3 server.py
```

Worth remembering that this ties the app to certificate renewal: when the cert
lapses, verification-on turns every showcase into a connection failure.

## Credentials

Every lane carries an `api-key-auth` policy, so **an unkeyed request is refused
401 before any policy runs** — which on the guardrail and spend-limit cards
would look like the policy firing when it never did.

The keys are **nine different values, not one repeated.** `api-key-auth` issues
keys per LLM provider and a key is rejected on any other provider, verified on
this gateway. One key spanning every lane would need `jwt-auth` instead, which
validates against a shared key manager rather than a per-provider key list.

Issue one key per provider in the AI Workspace, then paste them into
`config.env` — the wiring around them is already there, so these nine lines are
the only thing to fill in:

```bash
SHOWCASE_PLAIN_CHAT_API_KEY=
SHOWCASE_SPEND_LIMIT_API_KEY=
SHOWCASE_PROMPT_INJECTION_API_KEY=
SHOWCASE_SEMANTIC_CACHING_API_KEY=
SHOWCASE_TIME_BASED_ROUTING_API_KEY=
SHOWCASE_COST_BASED_ROUTING_API_KEY=
SHOWCASE_SEMANTIC_ROUTING_API_KEY=
SHOWCASE_PII_MASKING_API_KEY=
SHOWCASE_MODEL_FAILOVER_API_KEY=
```

`GATEWAY_API_KEY` is the fallback for any lane left blank — useful for a
single-provider deployment, and no use here where each provider issues its own.

**`config.env` is tracked, so those keys are one `git commit -a` away from being
published.** They are blank in git and must stay that way:

```bash
git diff -- ai-chatbot/config.env      # before every commit; expect no key values
```

If you would rather not carry that risk on a shared clone, keep the file's
committed state pinned locally:

```bash
git update-index --skip-worktree ai-chatbot/config.env
```

`server.py` never logs a key, and `/health` reports only `"keyed": true|false`
per showcase.

When copying the app to a host, **exclude the file** —
`rsync --exclude='config.env'`, as the command in
[Option A](#option-a--ssh-tunnel-recommended-nothing-to-open) does. Two reasons:
it keeps keys off the wire, and it stops the blank tracked copy from
overwriting a filled-in one already on the host. Keys in a demo front end are
cheap to rotate; revoke and reissue in the Workspace rather than moving a key
between machines.

## What the gateway must already have

The app never configures the gateway; if a lane is missing, its card refuses to
chat. Before a demo, confirm:

- **Nine providers exist**, at the context paths in
  [Credentials](#credentials), each with its policy already attached.
- **`api-key-auth` is attached to all nine**, and you hold a key for each. A
  key from one provider is rejected on the others.
- **The three routing lanes are attached at OPERATION scope** (`POST
  /v1/messages`). At global scope the gateway never injects `requestModel` and
  every request returns 500.
- **`claude-haiku-4-5` is registered on each provider.** It is the fast route's
  model and the routers' own default. Adding a model **redeploys the provider,
  which resets the cost-budget counter** — so add models before demoing
  cost-based routing, not during.
- **The PII lane needs nothing on the host.** `pii-masking-regex` reads no
  `config.toml` section, unlike the cache and semantic-routing lanes — attaching
  those two before their `[policy_configurations.*]` sections exist 500s the
  whole route.
- **Do not make `claude-opus-5` the model.** The gateway's pricing file has no
  entry for it, so `llm-cost` records every call as `$0.0000000000` and the
  budget never moves. `claude-opus-4-6` is priced, and is the deep route's
  model.

The provisioning tooling that creates and verifies this wiring is not in this
repo — it lives with the PoC's gateway work. Doing it by hand means editing the
LlmProvider artifacts through the Platform API, remembering that there is no
PATCH (405, so GET → mutate → PUT) and that GET never returns
`upstream.main.auth.value`, so the PUT omits the upstream key and the control
plane preserves the stored one.

## Deploying on the PoC host

The server **binds `127.0.0.1` deliberately**, and there is no flag to widen it.
`/health` publishes the resolved provider URLs for all nine lanes, and the app
has no authentication of its own, so a wider bind would make internal topology
readable to anything that can reach the port. Two honest options follow.

### Option A — SSH tunnel (recommended, nothing to open)

Same pattern as the observability UIs in the [root README](../README.md#reach-them-over-an-ssh-tunnel-and-only-that-way).
Copy the app up, run it, forward the port:

```bash
rsync -av --exclude='__pycache__' --exclude='config.env' \
  -e "ssh -i <this-client>-key.pem" \
  ai-chatbot/ ubuntu@<host-ip>:/opt/wso2/ai-chatbot/
```

`--exclude='config.env'` is deliberate. It keeps the keys off the wire, and it
stops a re-sync from replacing the host's filled-in file with the blank tracked
one — which would take a working demo down to 401s on every lane. The first
deploy therefore needs the file written on the VM by hand.

```bash
ssh -i <this-client>-key.pem -L 8600:localhost:8600 ubuntu@<host-ip>
```

Then, on the VM:

```bash
cd /opt/wso2/ai-chatbot && python3 -u server.py
```

Open <http://localhost:8600> on your own machine. `localhost` inside that
forward resolves **on the VM**, not locally.

For a session that outlives the SSH connection, run it under systemd —
`/etc/systemd/system/ai-chatbot.service`:

```ini
[Unit]
Description=AI chatbot demo
After=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/wso2/ai-chatbot
ExecStart=/usr/bin/python3 -u server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now ai-chatbot
```

**`python3 -u` matters.** Without it the startup banner — the wiring table you
actually want in the logs — sits unflushed in Python's stdout buffer and never
reaches `journalctl`, while per-request lines (written to stderr) appear
normally. Verified: same run, banner absent buffered and present with `-u`.

### Option B — exposed publicly

Only four ports are open in the security group, and the observability services
were deliberately kept out of it. Publishing the chatbot means opening a fifth,
which is a real decision, not a step:

1. Put a TLS reverse proxy in front (nginx/Caddy on the existing hostname) —
   the app speaks plain HTTP on loopback and terminates nothing.
2. **Block `/health` at the proxy**, or add auth in front of the whole app. It
   lists every internal provider URL.
3. Add authentication. The app has none, and each turn spends against the
   gateway's budgets — including the cost-based-routing budget the demo depends
   on being unspent.
4. Then open the port in the security group.

For a demo driven from a laptop, Option A is the right answer.

## Verify a deployment

```bash
curl -s localhost:8600/health | python3 -m json.tool
```

Expect `status: ok`, `catalogue: true`, `gatewayApiKeySet: true`, and a
`showcases` map with all nine `status: ready` **and `keyed: true`**. Any
`keyed: false` is a missing key and will 401 on first use. So is
`keySource: "fallback"` on any card but plain-chat: that card has no key of its
own and is borrowing `GATEWAY_API_KEY`, which is plain-chat's — the startup
banner lists these as "GATEWAY_API_KEY fallback only":

```bash
curl -s localhost:8600/health \
  | python3 -c 'import json,sys; [print(k, v["keyed"], v["keySource"]) for k,v in json.load(sys.stdin)["showcases"].items()]'
```

Then one real turn through the plain lane — it carries no cost policy, so it
disturbs no showcase budget:

```bash
curl -s -X POST localhost:8600/api/chat -H 'content-type: application/json' \
  -d '{"showcase":"plain-chat","messages":[{"role":"user","content":"Say: DEPLOY CHECK OK"}]}'
```

A healthy response is SSE: a `gateway_meta` event, then `message_start` naming
the served model (`claude-haiku-4-5-20251001` — the gateway answers a request
for `claude-haiku-4-5` with the dated id), then `text_delta` events.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every lane 401s with `Valid API key required` | No `config.env`, or no keys in it. The banner's `key` line and `/health`'s `keyed` flags say which. |
| One lane 401s, the rest work | That lane's key is missing, or a key from another provider was pasted into it — `api-key-auth` keys are not interchangeable. |
| Everything 401s despite keys being set | `GATEWAY_API_KEY_HEADER` is wrong. It must be `API-Key`, not `x-api-key`. |
| Only 1 of 8 lanes wired | `config.env` is absent — the lane contexts live only there, `showcases.json` carries none. |
| A card explains itself but refuses to chat | That lane is unwired. Check the startup banner for `MISSING`/`unwired` and the matching `SHOWCASE_*_CONTEXT`. |
| Every request on a routing card returns 500 | The routing policy is attached at global scope. It must be at operation scope (`POST /v1/messages`) or the gateway never injects `requestModel`. |
| 404 from the gateway on every card | `GATEWAY_BASE` points somewhere without those context paths. |
| Reply arrives empty, no error | `MAX_TOKENS` too small — thinking consumed the whole budget. |
| PII lane returns `*****` | The lane is running `redactPII: true` — one-way masking, and the card's Expect line is written for it. Not a fault. |
| PII lane returns the visitor's real address | The lane is running `redactPII: false` — reversible masking. Correct behaviour for that mode, but it contradicts the card's Expect line; check which mode you are demoing. |
| PII lane leaks `[EMAIL_0000]` to the page | Streaming got turned back on. Keep `SHOWCASE_PII_MASKING_STREAM=false` — the placeholder splits across SSE deltas and never matches. |
| PII lane refuses to echo the visitor's own data | `SHOWCASE_PII_MASKING_SYSTEM_PROMPT` is missing. The stock prompt makes the model decline, which is indistinguishable from masking having worked. |
| Cost budget never moves | The served model has no entry in the gateway's pricing file (`claude-opus-5`). |
| Cost chips missing entirely | Expected: `x-llm-cost` is not a response header on this gateway. `llm-cost` writes to shared metadata and only `x-ratelimit-cost-*` reaches the client, so the UI falls back to movement in the remaining balance. |
| Budget reads as spent right after a redeploy, or resets mid-demo | A provider redeploy resets the counter. Adding a model triggers one. |
| Certificate errors after enabling `TLS_VERIFY` | The Let's Encrypt cert lapsed or was replaced. Renew, or set it back to `false`. |
| Banner missing from `journalctl` | `ExecStart` lacks `-u`. |
| Port 8600 already bound | `--port 8601`, or stop the old process. |
