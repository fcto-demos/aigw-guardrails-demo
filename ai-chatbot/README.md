# AI Assistant — AI Gateway policy showcase

A branded chatbot that reaches Claude **only** through the WSO2 AI Gateway, and
a demo harness for the gateway's policies. The app holds no Anthropic
credential — the gateway does. That is the point of the demo, not an
implementation detail.

It does hold a **gateway** credential: every lane carries an `api-key-auth`
policy, so `server.py` sends a per-lane `API-Key` on each request. That key
authenticates the app *to the gateway*; it buys no access to Anthropic and
never reaches the browser. See [Nine keys, not one](#nine-keys-not-one).

A home screen lists nine showcases; each opens a chat screen carrying that
capability's scripted demo steps and the instrumentation that makes the policy's
effect visible. Deploying it? See [DEPLOY.md](DEPLOY.md).

The use case behind them is a **public, unauthenticated** visitor asking about
plans, roaming and eSIM. Nothing about the caller is known and nothing needs to
be: every message goes through the AI Gateway, and the gateway — not the app —
decides which model answers it, whether a model is called at all, and whether
the request is served at all. Every question in the step rail is verbatim from
that demo flow.

Account-shaped questions are deliberately outside it. Asked about a balance this
app says *"I can't see your account"*, and under this use case that is the
specification rather than a gap: every step that needs to know *which* customer
is asking was cut, along with the authorization it would rest on.

```
  browser                 this app                 AI Gateway              Anthropic
  ───────                 ────────                 ──────────              ─────────
  static/  ──/api/chat──▶ server.py  ──────────▶ /anthropic-…  ──────────▶ api.anthropic.com
   (SSE)   ◀─────────────  (relay)   ◀── SSE ───  + the Anthropic key      claude-haiku-4-5
                            + this                + that lane's policy     claude-opus-4-6
                            lane's                     │
                            API-Key             the model is chosen here
                                                       │
                                            the model is chosen here
```

The browser has no route to Anthropic, and no key to use if it had one — and
under gateway-side routing it has no say in which model answers either.

## Layout

```
ai-chatbot/
├── server.py            stdlib HTTP server: serves static/, relays /api/chat
├── plans.md             fictional product catalogue the assistant quotes from
├── showcases.json       the nine showcases: prose, demo steps, provider wiring
├── static/
│   ├── index.html
│   ├── styles.css       design tokens + layout
│   ├── app.js           entry point
│   ├── screens.js       home cards, hash navigation, mounting a showcase
│   ├── chat.js          one turn: POST, SSE reader, failure handling
│   ├── rail.js          the step rail and its expectations
│   ├── metrics.js       per-turn measurements and their chips
│   ├── spend.js         the estimated deep-model budget, for cost-based routing
│   ├── models.js        model-id matching, shared by the rail and the meter
│   ├── dom.js           element handles and DOM helpers
│   └── markdown.js      markdown-lite renderer
├── config.env           the PoC's lane wiring, with the nine API keys blank
├── config.env.example   annotated template, same keys documented at length
├── README.md            this file — what the app is, and how each showcase behaves
└── DEPLOY.md            how to run and deploy it, and what the gateway needs first

`config.env` is tracked, but its nine `SHOWCASE_*_API_KEY` values are
**deliberately blank** — paste yours in locally and do not commit that edit.

ES modules, loaded by the browser directly — no bundler, no build step, nothing
to install. Still Python 3 standard library only on the server side.
```

Python 3 standard library only — nothing to install. Tested on the system
Python 3.9.6.

## Running it

```bash
python3 server.py
```

Then open <http://localhost:8600>.

**Keys are now required.** `config.env` ships with the lane wiring already
filled in but the nine `SHOWCASE_*_API_KEY` values blank, so out of the box
every lane is refused:

```json
{"error":"Unauthorized","message":"Valid API key required"}
```

Paste a key per lane into `config.env` and all nine come up. (With no
`config.env` at all the app still starts and resolves all **9 of 9** contexts —
those are computed from `providers.json` + `showcases.json`'s contextSuffix,
not read from `config.env` -- but none are keyed, so every request 401s.) Real
environment variables override the file, and `--port` and `--model`
override both:

```bash
python3 server.py --port 8601 --model not-a-real-model   # useful for demoing the error path
```

`curl -s localhost:8600/health` reports the resolved configuration without
making a model call, including a `keyed` boolean per showcase — which is where a
missing key shows up *before* a 401 does.

## Nine keys, not one

Every lane carries an `api-key-auth` policy, so an unkeyed request is refused
401 by the gateway **before any policy runs** — which on the guardrail and
spend-limit cards would look like the policy firing when it never did.

The keys are **nine different values, not one repeated**: `api-key-auth` issues
keys per LLM provider and a key is rejected on any other provider, verified on
this gateway. One key spanning every lane needs `jwt-auth` instead, which
validates against a shared key manager rather than a per-provider key list. So
each lane gets `SHOWCASE_<ID>_API_KEY`, with `GATEWAY_API_KEY` as the fallback
for a single-provider deployment.

Two things to get right:

- **The header is `x-api-key`** — matching the policy's `key` parameter
  (`provision_showcases.py` provisions it as `X-API-Key`; HTTP header names
  are case-insensitive, and this gateway has always been driven with the
  lowercase spelling, verified repeatedly against the real thing). It happens
  to share a name with the header the *gateway* uses toward Anthropic, on a
  completely different hop; this app strips that one on the way back to the
  browser regardless, so there's no collision — just don't let the
  resemblance confuse a future rename.
- **The keys live only in `config.env`**, which is tracked with those nine
  values blank. They are gateway credentials, not Anthropic ones: a key
  authenticates one caller to one provider, and cannot configure the gateway or
  reach another lane. Filling them in is a local edit that must not be
  committed.

`/health` reports `"keyed": true|false` per showcase and never the key itself.

## What the assistant knows

`plans.md` is a fictional product catalogue — tariffs, add-ons, roaming zones,
top-ups, switching, eSIM — appended to the system prompt at request time. It is
the assistant's only source of product facts, which is what lets it answer
"what SIM-only plans do you offer?" with a real table instead of "I don't have
the live plan list". Edit the file and the answers change; no code involved.

Two guard rails come with it, both verified:

- it will **not invent** beyond the file. Asked for "a 250 GB plan for £3" it
  says no such plan exists and quotes the actual cheapest and largest tiers;
- it does **not disclose** the catalogue or the prompt. Asked to repeat its
  instructions verbatim it declines and offers to help instead.

Asked point-blank whether the prices are real, it does say they are demo
figures. That is deliberate — making it insist invented prices are genuine
would be building a deceptive demo, and it only volunteers this when directly
challenged.

Account-specific questions — balance, this month's usage — are still answered
with "I can't see your account", because the app has no billing system behind
it. Give it one by extending `plans.md`, or leave it as is.

Blank `PLANS_FILE` to restore the original deflecting behaviour.

## The showcases

Each showcase talks to its **own pre-configured LLM provider** on the gateway,
with that policy already attached. The app never configures the gateway — the
browser posts a showcase *id*, and `server.py` maps it to a provider URL and
that provider's own `API-Key`. No admin credential goes anywhere near the app —
an api-key-auth key authenticates one caller to one provider and can neither
configure the gateway nor read another lane.

That per-provider split also dissolves two sequencing constraints the use-case
document has to warn about: the routing provider has no cache attached, so the
cache cannot intercept a routing beat, and the spend limit's counter belongs to
its own provider, so exhausting it does not block the other demos.

| Showcase | Lane | Capability | Runs today? |
|---|---|---|---|
| Semantic routing | `/anthropic-semantic-routing` | `semantic-model-routing` | **Yes** — haiku for single facts, opus for comparisons |
| Time-based routing | `/anthropic-time-routing` | `time-based-model-routing` | **Yes** — peak 18:00–22:00 Europe/London |
| Cost-based routing | `/anthropic-cost-routing` | `cost-based-model-routing` | **Yes** — steps down to haiku at HTTP 200 |
| Model failover | `/anthropic-failover` | `model-round-robin` v1.1.1 | **Yes** — skips a model that keeps 5xx/429ing |
| Semantic caching | `/anthropic-cache` | `semantic-cache` v1.1.0 | **Yes** — non-streaming only |
| Prompt injection | `/anthropic-guardrail` | `regex-guardrail` v1.0.1 | **Yes** — refuses with 422 |
| PII masking | `/anthropic-pii-masking` | `pii-masking-regex` v1.0.4 | **Yes** — non-streaming only |
| LLM spend limit | `/anthropic-cost-limit` | `llm-cost` + `llm-cost-based-ratelimit` | **Yes** — refuses with 429 |
| Just chat | `/anthropic` | none under test | **Yes** |

All nine lanes run on **stock AI Gateway 1.2.0 policies** as of 2026-09-10. The
three routers are attached at **operation** scope (`POST /v1/messages`), which is
the whole reason they work.

All three policies require a `requestModel` system parameter that the gateway
controller is documented as injecting from the LLM provider template — and it
does, but **only for operation-scoped attachments**. A global attachment binds to
a wildcard route spanning every path in the provider spec (`/v1/messages`, but
also `/v1/files`, `/v1/organizations/users` and ~30 more), so there is no single
operation whose template mapping could be applied and nothing is injected. The
gateway then fails chain build for every method on the route:

```
[pol] level=ERROR msg="Failed to build policy chain for route, skipping"
  route=POST|/anthropic-time-routing/*|*
  error="failed to create policy instance time-based-model-routing:v0:
         invalid params: 'requestModel' system parameter is required"
```

and every subsequent request returns 500. Scoped to the concrete operation
instead, the same policy on the same provider builds cleanly and routes.

Worth knowing beyond this app: **`model-round-robin` v1.1.1 has the same
defect**, and it is GA and the documented failover construct for 1.2.0.
Attaching it globally — the UI's obvious path — makes every request on that
provider 500, and nothing warns you.

Each router gets a lane of its own rather than sharing one, because each carries
its own model mapping and a shared lane would stack three overrides on every
request. Cost-based routing is the graceful half of cost control: where the
spend limit refuses outright at zero, this one degrades quality on the way down
— an answer on a cheaper model instead of a 429.

Fallback routing was dropped from the use case, so there is no card for it.

Every chat screen opens on the same greeting — *Hi, how can I help?* — whichever
capability is being shown. These cards are meant to look like an ordinary
assistant while the gateway does the work behind it; a per-showcase heading
would announce the policy to someone who is supposed not to notice it. What the
capability is, and whether it can run here, is the rail's job and the notice
strip's.

A card whose policy the gateway does not carry is badged with exactly what is
missing, opens, explains itself and lets the script be rehearsed — but refuses
to send. So is a card with no provider wired. Neither falls back to the plain
provider, because a card that answers normally with no policy attached is a demo
that lies. Nothing is in that state today; the machinery stays because the next
policy to be tried will be.

**Wiring a showcase** is one line in `config.env`:

```
SHOWCASE_PROMPT_INJECTION_CONTEXT=/anthropic-guardrail
```

`curl -s localhost:8600/health` prints the resolved URL for every showcase and
which config key supplied it. The startup banner prints the same table, and
flags a showcase that is marked ready but has no provider in red — that
combination is an operator bug, not an expected gap.

### Demo steps, and what the app will and will not claim

Each showcase's steps come from `showcases.json`, which holds the prose and the
verbatim questions so a presenter can reword a beat without touching code.

**Each step hands its prompt over rather than sending it.** *Copy prompt* puts
the question on the clipboard and the presenter pastes it into the composer, so
the audience sees a question being asked rather than a button being pressed —
and the prompt can be edited on the way in. The rail still grades the result:
a submitted question is matched back to the step it came from by its text, and
where the same question appears in two steps (time-based routing asks the
identical one twice on purpose) it lands on whichever has not run yet.

Copying falls back from `navigator.clipboard` to `execCommand`, and finally to
selecting the text with a *press ⌘C* prompt — with no Send button it is the only
way a prompt gets out of the rail, so it must not have a single point of failure.

A step declares what to expect, and the rail grades only what it can actually
measure:

- `model` — the served model, from `message_start`
- `blocked` / `gatewayStatus` — the gateway's own verdict
- `differsFrom` — two answers must not be identical. This is the semantic-cache
  negative control, and it catches the exact failure the use-case document warns
  about: at the default `similarityThreshold` of `0.5`, "how much is the 30 GB
  plan" and "the 100 GB plan" collide and the second returns the first's answer
- `fasterThan` — one step must be measurably faster than another. Still
  supported, but **no card uses it**: it was grading the cache hit, and a slow
  network was failing the step on turns `X-Cache-Status: HIT` had plainly
  served. The latency is reported in the metrics strip instead

Anything else — "the answer should mention Spain as Zone A" — is presenter prose
in a collapsed disclosure, and the step reads a neutral *ran · served by …*. A
step may also declare **no expectation at all**, and reads the same way: the
cost-routing card is built like that on purpose, because which of its four sends
crosses the budget depends on what that lane already spent this hour, and a step
asserting a model would go red on the beat where the policy was working. **A green tick
that really means "it ran" is worse than no tick, because on stage it will be
believed.** There is deliberately no "run all steps" button: the caching card's
first step has to miss before the second can hit, and the spend-limit card
exhausts a provider-wide budget.

## The gateway picks the model, not the app

The gateway deciding this, and not the app, is the whole point of the use case —
so the app is built to make that visible rather than to work around it.

Anthropic's Messages API requires a `model` field, so `server.py` sends one —
`MODEL`, default `claude-haiku-4-5`. It is a **default the gateway's routers
override**, not a choice. Three policies override it, one per lane:

| Lane | Policy | Decides from | Models |
|---|---|---|---|
| `/anthropic-semantic-routing` | `semantic-model-routing` | what the question means | `claude-haiku-4-5` for single facts, `claude-opus-4-6` for comparisons |
| `/anthropic-time-routing` | `time-based-model-routing` | the gateway clock | `claude-haiku-4-5` in peak (18:00–22:00 Europe/London), `claude-opus-4-6` outside it |
| `/anthropic-cost-routing` | `cost-based-model-routing` | accumulated spend | `claude-opus-4-6` while its budget holds, then `claude-haiku-4-5` |

All three are live on the PoC gateway on stock 1.2.0 policies. Three tuning
notes that measurement forced, all easy to get wrong:

- `semantic-model-routing`'s `scorethreshold` is 0.75, not the schema default
  0.90, which sends everything to `defaultModel` without erroring.
- `cost-based-model-routing`'s fallback model needs a budget entry of its own
  with capacity, or an exhausted deep budget returns 429 instead of stepping
  down.
- **The cost lane is the one place the model the app sends still matters.** It
  keys on `requestModel` (`$.model` in the payload) and looks up *that* model's
  budget, so a request naming `claude-haiku-4-5` matches the economy budget
  ($1/24h), never exhausts it, and the card serves the fast model from the first
  call with no step-down to show. That lane therefore carries
  `overrides.model: claude-opus-4-6` in `showcases.json`. The time lane needs no
  override because its `fallback` names the deep model, and the semantic lane
  names a model on every route — for those two the app's model really is inert.

Three consequences for this app:

- **There is no model picker.** The `MODELS` allowlist is gone with it: an
  allowlist only means something if the client's choice reaches the gateway,
  and it no longer does. Anything the browser puts in `model` is discarded by
  `server.py` before the request goes out.
- **The decision is read back, never asserted.** Anthropic's `message_start`
  event carries the `model` that actually served the reply. The control strip
  shows it after *Answered by*, and each assistant bubble carries it as a
  footer — so two adjacent replies on different models are visible side by
  side, which is what beats 2 and 3 of the demo turn on. The strip drops the
  dated snapshot suffix (`claude-haiku-4-5-20251001` → `claude-haiku-4-5`) so
  the row still fits the provider tag and the cost meter; the full id stays on
  the bubble footer and in the readout's tooltip.
- **Changing model no longer clears the transcript.** It used to, on the
  reasoning that mixing models made the cost reading meaningless. Under routing
  the model legitimately changes per message — a simple question and a
  comparison in the same conversation are *meant* to land differently — so
  clearing would wipe the transcript mid-demo.

`claude-haiku-4-5` must be registered on the LLM provider. The hosted provider
shipped with `claude-opus-5` and `claude-opus-4-6` only. Adding a model
redeploys the provider and resets the cost-budget counter — harmless, but do it
before the demo rather than during it.

Do not set `MODEL=claude-opus-5`: the gateway's pricing file has no entry for
it, so `llm-cost` records every call as `$0.0000000000` and the budget readout
never moves.

## What each turn reports

Every reply carries a chip strip: time to first token, total elapsed, input and
output tokens, and the served model. Which chips are emphasised is per showcase
— routing leans on the model and the clock, the guardrail on **zero tokens**,
the spend limit on the budget moving.

Two chips are specific to cost-based routing, where the gateway reports no cost
at all: *~$0.0090 this call*, computed from the tokens and the gateway's own
price file, and *stepped down · the claude-opus-4-6 budget is spent* on the turn
the fallback first answers. The `~` is not decoration — see
[Cost-based routing keeps its own meter](#cost-based-routing-keeps-its-own-meter-and-it-is-an-estimate).

The token counts come from Anthropic's own `usage` fields. One trap worth
knowing: `usage` sits at the **top level** of `message_delta`, not inside
`delta`, so a reader that only descends into `event.delta` never sees the output
count. `_relay_whole()` synthesises it in the same position, so one read covers
both paths.

A blocked turn has no bubble to hang metrics on — both are removed — so the
strip appears in the error panel instead. On the guardrail card that panel is
the entire payload: *blocked before the model · 0 tokens*. It never shows a
cost, even if the gateway sends an `x-llm-cost` header on the 400: putting a
price on a call that never happened would undercut the one beat whose whole
claim is that no call happened.

### Cache-hit detection is a guess, and says so

**No cache-hit header was documented anywhere** in the gateway distribution or
the use-case document, so the app forwards every `x-*` response header and shows
them raw in a collapsed disclosure in the rail — which is how the real one was
found: this gateway sends **`X-Cache-Status: HIT`**, and that is now the
card's primary evidence. The inference below stays as the fallback for a gateway
that sends nothing:

- a header that reports a hit gives a **solid** badge;
- suspiciously low latency with no output tokens gives a **dashed** badge whose
  own text reads *cache hit? inferred from latency*, because a tooltip is
  invisible on a projector;
- a miss is **never** inferred. Absence of evidence renders nothing.

Latency is still reported — 2.3–4.8s for the model call against 0.68–0.78s from
cache on this gateway, a 3–6× drop — but it is **no longer graded**. A slow
network, or a warm-but-slow run, was failing the step on turns `X-Cache-Status:
HIT` had plainly served. The header is the verdict; the speed is the colour.

## Watching the gateway's cost budget

The provider carries `llm-cost` and `llm-cost-based-ratelimit`. Two things about
that pair are load-bearing for reading this UI: `llm-cost` writes the
per-request cost into the gateway's shared context under `x-llm-cost` and
`llm-cost-based-ratelimit` reads it back from there, so `llm-cost` must come
first in `globalPolicies`; and **both run in the response phase**, so the budget
trips on the request *after* the one that exhausts it, never the one that does.

This app surfaces that so you can test it by hand:

- a **budget readout** — `$0.0464 of $0.05 left` with a bar that turns amber
  under 25% and red at zero;
- a **429** renders as *Cost budget exhausted*, with the gateway's own body and
  your question handed back to the input box.

The bar resets when you **switch showcase**, because each showcase is a
different provider and `llm-cost-based-ratelimit` counts per provider — carrying
a balance across would show one provider's budget on another's screen. *Clear
chat* deliberately keeps it: there the provider has not changed.

The server forwards the policy's `x-ratelimit-cost-*` headers to the browser as
a synthetic `gateway_meta` SSE event ahead of the model's own events.

### Cost-based routing keeps its own meter, and it is an estimate

The spend-limit card reads its balance off the gateway. The cost-based-routing
card cannot: that lane carries `cost-based-model-routing` and `llm-cost`, and
neither publishes one. The router emits no remaining-budget header by design —
the spend is not known until the body completes — and `llm-cost`, unlike
`llm-cost-based-ratelimit`, writes its figure into gateway metadata rather than
a response header. There is nothing to read.

So `static/spend.js` computes it: the token counts Anthropic returns, multiplied
by the per-token prices from the gateway's own
`configs/llm-pricing/model_prices.json` — the same file `llm-cost` charges the
budget from. `showcases.json` mirrors the provider's `budget` block:

```json
"budget": {
  "model": "claude-opus-4-6", "fallbackModel": "claude-haiku-4-5",
  "amount": 0.02, "duration": "1h",
  "prices": { "claude-opus-4-6": { "input": 5e-06, "output": 2.5e-05 } }
}
```

**Change the provider's budget and change this too**, or the meter drifts away
from the policy it is describing.

Two rules keep the meter honest, and the UI holds both:

1. Every figure is prefixed `~`, the bar is dashed, and the text says
   *estimated*. The budget is shared across all callers on a fixed window, so a
   rehearsal ten minutes ago already spent some of it — this session's tokens
   are a **floor** on the real spend, never the balance.
2. **The step-down is measured, not estimated.** The router serves the deep
   model whenever that budget has capacity, so a reply from the fallback means
   the capacity is gone whatever the arithmetic says. The observation wins: the
   bar pins to empty and reads *budget spent · serving haiku-4-5*, and that turn
   — only that turn — gets a *stepped down · the claude-opus-4-6 budget is
   spent* chip. That is also the
   honest answer on the first call of a session, when an earlier run already
   spent the window and a purely arithmetic meter would be at its most
   confidently wrong.

The spent state is **amber, not red**: the gateway answered, on a cheaper model.
Red stays reserved for *the gateway said no*. A deep-model reply arriving after
a step-down means the window rolled over, and the running total starts again.

At roughly $0.009 per opus reply against a $0.02/1h budget — and with the router
reading the balance *before* the call — the step-down lands on the third or
fourth send. So the card needs no operator action mid-demo, which is why its
steps no longer carry one.

### The side-by-side that makes the point

Not surfaced in the UI any more — it is an operator-facing detail, so it lives
here rather than in the intro copy.

| Model | What the budget does |
|---|---|
| `claude-opus-4-6` | falls ~$0.0015 per reply — it is in the gateway's pricing database |
| `claude-opus-5` | does not move; it is **absent** from that database, so cost computes as $0 |

Measured: two `claude-opus-4-6` replies took $0.05 → $0.0478 → $0.0464, while
two `claude-opus-5` replies left it unmoved.

**The window is fixed, not rolling.** Once the hourly quota reaches zero it
stays at zero — sampled every 28s for 90s with `x-ratelimit-remaining: 0`
throughout — until `x-ratelimit-reset`, then the full budget returns at once.
There is no gradual refill to wait out. (An earlier note here claimed the
opposite; a small upward tick in the remaining balance was a window rollover,
not a trickle.)

**Exhausting it blocks the whole provider.** Because `consumerBased` is `false`
there is one counter for everything behind `/anthropic`, so running out on
`claude-opus-4-6` also refuses `claude-haiku-4-5` — and refuses anyone else using
that gateway, since the budget is shared across all callers, not per client.
At $0.05/hour that is easy to hit while testing.

### Making the limit trippable by hand

At $0.05 you would need ~30 replies to hit the wall. To demo the refusal,
temporarily shrink the `llm-cost-based-ratelimit` budget on the `anthropic`
provider to something like `0.002:1h`, exhaust it in two messages, then restore
it to `0.05:1h,1:24h`.

That edit happens on the gateway, not here — this app never configures a policy.
Three things about that edit are worth knowing, because each is easy to get
wrong:

- **There is no PATCH.** The Platform API answers 405, so a policy change is
  GET, mutate, PUT against the LlmProvider artifact.
- **The PUT cannot resend the upstream API key**, because GET never returns
  `upstream.main.auth.value`. That is safe: the control plane preserves a stored
  api-key value when the field is absent from the PUT.
- **Each change redeploys the provider**, which takes a few seconds and **resets
  the counter** — so restoring the real budget also hands you a full one, and an
  unrelated redeploy mid-demo will silently do the same thing.

Note the budget is **shared across the whole provider** (`consumerBased:
false`), so exhausting it on the deep route also blocks the fast one — and
blocks every other caller of that gateway.

## PII masking, and which mode the lane is in

The `pii-masking-regex` lane replaces personal data before the request leaves
the gateway, so Anthropic never receives it. **What comes back depends on one
policy parameter**, and the card's Expect line is only correct for one of the
two settings:

| `redactPII` | The model sees | The visitor sees | Card's Expect line |
|---|---|---|---|
| `true` (live on this gateway) | `*****` | `*****` — one-way, nothing to reverse | correct |
| `false` (script default) | `[EMAIL_0000]` | the real address, substituted back | contradicted |

Measured on 2026-09-10 against the live lane, which runs `redactPII: true`: an
email and a mobile were both replaced with `*****` and nothing was substituted
back. Asked to repeat the address the lane answered *"Yes, your message contains
an email address: \*\*\*\*\*"*, and on another run the model said outright that
"the asterisks are showing as placeholders rather than actual numbers or
characters" — it received the asterisks, not a token. The step's Expect line now
matches that: the address must come back **masked**.

Flip the lane to `redactPII: false` and the same prompt returns the real
address, at which point the step contradicts itself. **Check which mode the lane
is in before demoing it.**

Three settings on the app's side are measured, not preferences:

- **`SHOWCASE_PII_MASKING_STREAM=false`.** Under reversible masking, unmasking
  is a substring replace, and over SSE the placeholder gets split across deltas
  — `["My", " email is [EMAIL", "_0000]"]` — so it never matches and
  `[EMAIL_0000]` reaches the client. Measured over three streamed sessions: 1/6,
  4/6 and 0/6 runs restored correctly; non-streamed was 4/4 and clean since.
  Keep it off in either mode.
- **A permissive system prompt.** The model refuses to spell out or transform
  anything resembling a real email, card or NI number — and a refusal is
  indistinguishable from masking having worked, which kills the demo. The lane's
  prompt is the stock one plus a clause that removes the refusal without
  changing the assistant's persona, so the same lane still answers an ordinary
  catalogue question warmly.
- **Only the current turn is masked.** The policy's `jsonPath` is
  `$.messages[-1].content`, and this app replays history — so the card is
  deliberately a single self-contained turn.

Unlike the cache and semantic-routing lanes, `pii-masking-regex` reads nothing
from `config.toml`, so it needed no change on the gateway host.

**Reversible masking needs a transform to be visible at all.** Asking for the
value back verbatim cannot demonstrate it — and "give me the text between the
square brackets, without the brackets" defeats restoration 5/5, because the
gateway's substitution matches the placeholder *including* its brackets.

## A refusal is not a fault

`regex-guardrail` v1.0.1 refuses with **HTTP 422** — not the `400` its docs and
the UC2 design imply — and returns a structured body carrying
`GUARDRAIL_INTERVENED` / `REGEX_GUARDRAIL`. Verified on the hosted gateway. A
client hardcoding 400 as "blocked" paints a correct guardrail refusal as an
upstream fault, so the showcase declares `"blockStatus": 422` and
`POLICY_BLOCK_MARKERS` names the two marker strings.

On that card the refusal **is** the demonstration. So gateway errors are
classified, and the two deliberate refusals get a status that is not alarming:

| `gatewayOutcome` | Meaning | Status the app returns |
|---|---|---|
| `policy_block` | a policy refused on purpose | 422 |
| `budget_exhausted` | 429, the spend limit working | 422 |
| `upstream_error` | 401, 404, 5xx | 502 |
| `unreachable` | cannot reach the gateway | 502 |
| `not_configured` | the showcase has no provider | 503 |

A refusal renders in a navy/lavender strip rather than the red error treatment.
The gateway's `gatewayStatus` and `gatewayBody` are always preserved verbatim,
so nothing is hidden even when the classification guesses wrong.

## The client cannot choose a provider

The browser posts a showcase **id**, and the server looks it up in its own
registry. The id is used as a dict key and nothing else — never joined into a
URL, a path or a filename.

This matters more than it looks. The relay exists precisely *because* the
browser cannot reach the gateway (no CORS, an untrusted certificate). A relay
that accepted a URL from the page would hand back exactly the reach it was
denied, against every host the process can see. Unknown ids are rejected 400,
loudly, rather than falling through to a default.

Two related guards: `x-*` headers matching
`key|token|secret|auth|credential|passw|signature|cookie|session` are never
forwarded to the browser — `x-api-key` is the gateway's own auth header name
toward Anthropic and `API-Key` is the one this app sends inbound, so a
misconfigured `set-headers` policy echoing either would otherwise be relayed
straight to the page. And header values are escaped before rendering, because a
guardrail assessment can quote the visitor's own prompt back.

## Why there is a server and not just a page

The browser cannot call the gateway directly:

- the data plane sends no CORS headers, so a `fetch` from a page is blocked;
- the hosted gateway presents a certificate a page on `localhost`
  cannot wave through;
- every lane now carries an `api-key-auth` policy, and that key must not ship
  to the browser. This stopped being hypothetical: `server.py` holds nine of
  them.

`server.py` is a thin relay. It shapes the request, forwards the stream, and
adds no logic of its own.

## Verified behaviour

Run against the live hosted gateway on 2026-08-31:

| Check | Result |
|---|---|
| `POST /api/chat`, single turn | 200, `text/event-stream`, 8 SSE events, full answer |
| Streaming | Text paints progressively; the gateway relays SSE unchanged |
| Multi-turn memory | Recalled a name and plan size given two turns earlier |
| System prompt | In effect; `{brand}` substitution confirmed in the assembled prompt |
| Markdown | Bold, bullets, ordered lists, code and **pipe tables** render; no stray asterisks or pipes |
| Thinking blocks | Never reach the bubble; no signature text leaks into the page |
| Unknown model | Gateway's verbatim `404 not_found_error` shown in the error strip, question handed back |
| Malformed input | Empty / non-JSON / bad-role bodies all rejected with 400 |
| Path traversal | `GET /../server.py` → 404 |
| Mobile at 375px | No horizontal overflow |
| Model allowlist | A request for `evil-expensive-model` fell back to the default; the gateway never saw it |
| Budget readout | Fell $0.05 → $0.0478 → $0.0464 over two `claude-opus-4-6` replies |
| Unpriced model | Two `claude-opus-5` replies left the budget still |
| 429 rendering | Shows *Cost budget exhausted*, bar red, gateway body verbatim, question restored |
| Rebrand | Wordmark, page title, placeholder and prompt all read `BRAND_NAME`; no residual brand in the served page |
| Plan answers | Quotes the catalogue: full tariff table, correct price for "the 30GB plan", and a sensible recommendation for "12GB a month, travels to Spain" |
| No invention | "A 250 GB plan for £3?" → says no, and cites the real cheapest and largest plans |
| No disclosure | Refuses to reproduce the system prompt or mention the catalogue |
| Brand-free answers | Four probes, including "compare yourself to <real operator>" and "name three UK networks": the model refuses to name or compare other operators, gives no external URL, and does not echo a brand back from the question |
| Mid-stream upstream error | Anthropic's `overloaded_error` event now surfaces as *Upstream error* with its message, instead of an empty "no text" notice |

The client-side routing behaviour was verified on 2026-09-04 against a local
mock gateway that answers Anthropic SSE and deliberately overrides the requested
model — which is exactly what the three routers now do on the PoC gateway. The
mock rows still stand, because what they check is the page's reaction to a model
it did not ask for:

| Check | Result |
|---|---|
| Client cannot choose a model | A body carrying `"model": "evil-expensive-model"` went upstream as the configured default; the mock recorded `model='claude-haiku-4-5'` |
| `/api/config` | Returns `brand` and `defaultModel` only — no model list for a picker to build |
| Answered-by readout | Control strip and assistant bubble both show the model from `message_start`, not the one sent |
| Model changing mid-conversation | Two consecutive replies served by different models were each labelled correctly, in one unbroken transcript — nothing cleared |
| Clear chat | Resets the readout to *no reply yet*; the budget bar, which is gateway state, stays |
| Mobile at 375px | Control strip fits label-free with the full model id and the budget; no horizontal overflow |
| Console | No errors |

### The showcase harness

Verified on 2026-09-07, when four of the eight lanes did not exist yet.
Everything below except the two rows that name the live gateway was exercised
against a local mock standing in for the providers — it overrides the served
model, emits `usage` on `message_start` and top-level on `message_delta`, sets
assorted `x-*` headers including credential-shaped and `<script>`-bearing ones,
and returns 400 and 429 on demand.

**Not yet re-run since the routing and caching lanes went live** (2026-09-08 /
2026-09-09): the three routing cards and the caching card have been verified at
the *gateway* by the provisioning tooling's own verify pass and by direct
`curl`, but this app's own run against them — the step verdicts, and the
cost-based card's estimated meter and step-down chip — needs one pass by hand. The meter in particular has never been watched against a real budget: its
arithmetic is unit-checkable, its agreement with what `llm-cost` actually charges
is not.

| Check | Result |
|---|---|
| Registry | `showcases.json` parses; `/api/showcases` returned all showcases then present, with correct `status` / `configured` / `configKey` |
| No topology leak | The registry payload carries the provider **path** only — no host, no port, no resolved URL |
| SSRF guard | `../evil`, a full URL, `/anthropic`, a 300-char id, a number and an object in the `showcase` field were all rejected 400, and **no upstream call was attempted** for any of them |
| Unknown id | Rejected 400 with the known ids listed — never a silent fall-through to the default |
| Unwired showcase | Server 503s with the exact config key to set; the client refuses first, so no request is made |
| Backward compatibility | A body of `{messages}` with no `showcase` still relays 8 SSE events on the default provider — today's client shape works unchanged |
| Credential stripping | The mock's `x-api-key` and `x-session-token` were dropped; `x-llm-cost`, `x-request-id` and `x-envoy-upstream-service-time` came through |
| Header XSS | A header value of `<script>alert(1)</script>` renders as text — no live `<script>` element in the DOM |
| Guardrail block | Re-verified 2026-09-07 against the **live** `/anthropic-guardrail` provider: HTTP 422 classified as `policy_block`, **no reply bubble at all**, navy refusal strip, *blocked before the model · 686 ms · 0 in / 0 out tokens*, no cost chip, the real `GUARDRAIL_INTERVENED` body shown verbatim, question handed back |
| Copy prompt | Every step button copies its exact prompt; a real click reports *Copied*. The clipboard **round trip could not be verified through the test automation** (synthetic ⌘V does not paste), so confirm it by hand once |
| Step attribution | A prompt pasted into the composer is matched back to its step and graded — verified on the guardrail card (*passed*) and the spend-limit card (*ran · served by …*) |
| Uniform greeting | Every showcase shows *Hi, how can I help?* and the same body copy |
| Budget refusal | HTTP 422 / `budget_exhausted`; *Cost budget exhausted*, bar red, question handed back |
| Per-turn metrics | Real values off the mock: 134 ms to first token, 386 ms total, 1204 in / 318 out tokens |
| `differsFrom` | Two identical answers correctly failed the step: *"the similarity threshold is too loose; raise it and redeploy"* |
| `fasterThan` | A non-faster reply correctly failed: *"only 1.2× faster than step 1 — that is not a cache hit"*. The predicate still works; the caching card no longer declares it |
| Uncheckable steps | Read a neutral *ran · served by …*; an unexpected refusal on such a step still reports the refusal and fails |
| Budget lifecycle | *Clear chat* keeps the bar; switching showcase resets it, the route readout and the transcript |
| Thinking blocks | Still never reach the bubble after the module split |
| Deep links | `#/<id>` mounts the showcase directly; the back button returns; home cards are anchors |
| Rail slide-over | Below 1024px it is a slide-over with a *Steps* toggle, closes on Esc and on sending a step |
| Widths | No horizontal overflow at 375, 900 or 1440px |
| Console | No errors |

## Gotchas worth knowing

**`max_tokens` must be generous, or you get silence.** A reasoning model spends
output tokens on internal reasoning *before* it writes anything — and the deep
route is where one lands. With
`max_tokens: 32` this gateway returned a `thinking` block, no text at all, and
`stop_reason: max_tokens` — a blank reply, not an error. The default here is
2048, and the UI says so explicitly if it ever happens.

**Answers are full of tables.** Once the assistant has a catalogue it replies
with pipe tables constantly, so `renderProse()` has to understand them — before
that they came out as a wall of raw `|` characters. Wide tables scroll inside
their bubble via `.table-wrap` rather than stretching the column.

**The served model comes back dated.** A request naming `claude-haiku-4-5` is
answered by `claude-haiku-4-5-20251001` — verified on the hosted provider. A
step expectation comparing model ids exactly would therefore fail every routing
beat against the real gateway while passing against a mock, so `evaluateExpect`
matches on the family prefix at a dash boundary instead.

**`x-llm-cost` is not a response header on the live gateway.** `llm-cost` writes
the per-request USD into *shared metadata*, and the hosted provider does not
surface it to the client — only `x-ratelimit-cost-*` comes through. So the
per-call cost chip is absent there and the UI falls back to the movement in the
remaining balance. This is why the derived `cost` / `cacheHit` fields on
`gateway_meta` are documented as allowed to be absent: the raw `headers` map is
the contract, and anything computed from it is a convenience.

**`message_start` is the only place the routing decision appears.** Nothing
else in the stream names the model, and the app deliberately has no other
source for it — asserting the model client-side is precisely what this use case
removes. A reader that skips `message_start` (it carries no text, so it is easy
to treat as noise) leaves the readout permanently on *no reply yet*. The
non-streaming fallback in `_relay_whole()` synthesises the same event from the
JSON body, so both paths report the served model.

**Only `text_delta` may be painted.** The stream also carries `thinking_delta`
and `signature_delta`. A reader that renders every delta, or that takes
`content[0]` from a non-streamed response, will show the model's reasoning in
the chat bubble. `app.js` filters on `delta.type === 'text_delta'`.

**Thinking blocks are never sent back.** The client stores and replays text
only, and a reasoning block replayed without its signature is rejected. This is
the same hazard that broke Gemini tool-calling elsewhere on this PoC: every
current Gemini model rejects a conversation containing a `functionCall` part
that has no `thought_signature`, and `openai-to-gemini-transformer` v0.9.0 does
not surface that signature in the OpenAI-shaped response — so there is nothing
to send back and the replay fails 400. For a tool-free chatbot, dropping the
block is simply correct.

**A failed turn must leave no trace in the history.** Anthropic rejects two
consecutive `user` messages, so a question with no answer beside it cannot be
carried forward. On any gateway failure the app removes both bubbles, drops the
history entry, and puts the question back in the input box.

**The gateway relays SSE.** Confirmed on the wire: `content-type:
text/event-stream`, `server: WSO2 API Platform`. `_relay_whole()` used to be
dead code against this gateway; it is not any more. Two lanes now declare
`stream: false` — semantic caching and PII masking — so the gateway answers them
with JSON and that path carries them, synthesising the same events the client
already reads. Verified `"streamed": false` on the PII lane.

**Cost headers vanish on a 429.** Once the gateway starts refusing, it stops
reporting `x-ratelimit-cost-limit-dollars` / `-remaining-dollars` at all. The UI
treats missing values as *unknown*, never as zero — otherwise a single unrelated
error would wrongly paint the budget as spent.

**No LLM proxy is involved.** This app speaks Anthropic's native Messages
dialect straight to the provider's own context path, `/anthropic`. An
`openai-to-anthropic-transformer` LLM proxy would only be needed for a client
locked to the OpenAI Chat Completions dialect.

## Notes

- The palette is magenta `#DB00AF`, navy `#0D1167`, lavender `#E7EBFF`, mint
  `#1EE98A`, set in **Figtree**.
- The product name is a single config value, `BRAND_NAME` (default
  `Acme Mobile`). It drives the wordmark, the page title and the system prompt;
  nothing else hard-codes a name. The system prompt also forbids the model from
  naming or comparing real-world operators, so no real telecom brand appears in
  answers either.
- `TLS_VERIFY` defaults to `false`, from when the hosted host's certificate was
  untrusted. It now presents a valid Let's Encrypt certificate and a full
  streamed turn has been verified with verification **on**, so `true` works
  today — at the cost of tying the demo to certificate renewal. The server
  prints a warning at startup while it is off.
- The transcript lives in the page only. Reloading, or pressing **Clear chat**,
  starts over; nothing is persisted anywhere.
- This is a demo front end. It has no authentication of its own and binds to
  `127.0.0.1` deliberately.
