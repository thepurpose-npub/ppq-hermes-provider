# ppq-hermes-provider

Hermes model-provider plugin for **PayPerQ** ([ppq.ai](https://ppq.ai)) — an
OpenAI-compatible inference relay with per-response cost estimation and
opt-in privacy routing (Zero Data Retention / TEE).

> **Status:** draft for publication — contents below are a proposal and have
> **NOT** been pushed or published anywhere yet.

## What this plugin adds on top of a plain OpenAI-compatible provider

- **`ppq` provider profile** in the Hermes model picker (aliases:
  `payperq`, `ppq-inference`; base URL `https://api.ppq.ai/v1`).
- **Chat model discovery** via `GET /v1/models`, filtered to chat-type
  models, with a **3-layer pin fallback** that survives a failed fetch:
  user-local pins (`~/.hermes/ppq-plugin/pins.json`) → live "top chat
  models" from ppq's public stats dashboard → a small generic static list.
  Generic installs never see the author's personal pins (see
  [Customizing model pins](#customizing-model-pins)).
- **Per-response cost estimation.** ppq's relay endpoint is deliberately
  unpriced by Hermes' bundled tables (custom base URLs never inherit a
  vendor list price), so the plugin reads a local snapshot of ppq's
  published rate card (`$HERMES_HOME/ppq-pricing/prices.json`, refreshed
  every 2 days by an optional `ppq-price-watch` cron script — not shipped
  in this repo) and prices each response from it. Without the snapshot,
  responses are honestly reported as `unknown cost` rather than guessed.
- **Automatic Zero-Data-Retention opt-in** for badged models (see below).
- **TEE/e2e documentation** of ppq's enclave-covered model tier.

## Requirements

- Hermes >= 0.21.3 (tested on 0.22.0)
- `PPQ_API_KEY` environment variable (optional: `PPQ_BASE_URL` to override
  the relay endpoint)

## Install

**Via the Hermes plugin catalog (recommended, once merged):**

```bash
hermes plugins install ppq-provider
```

The catalog entry pins an immutable commit; to install a specific commit
instead, use `hermes plugins install ppq-provider --ref <40-char-SHA>`
(manifest: `NousResearch/hermes-agent` → `plugin-catalog/ppq-provider.yaml`).

**Manual:**

```bash
git clone https://github.com/thepurpose-npub/ppq-hermes-provider
mkdir -p ~/.hermes/plugins/model-providers/ppq
cp ppq-hermes-provider/model-providers/ppq/{__init__.py,plugin.yaml} \
   ~/.hermes/plugins/model-providers/ppq/
```

Then set `PPQ_API_KEY` and restart the Hermes gateway so the plugin loads.

## Customizing model pins

The model picker's curated "pinned" block (which leads ahead of the live
catalog) resolves in three layers, checked in order:

1. **User-local override** — if the file `~/.hermes/ppq-plugin/pins.json`
   exists and is a non-empty JSON list (or `{"models": [...]}`) of model
   ids, those ids are used **entirely**, replacing both layers below. This
   file lives outside the plugin directory on purpose, so plugin
   upgrades/reinstalls never wipe it. This is how you keep personal pins
   (e.g. `private/*` e2e endpoints that only exist for your account).
2. **Live top models** — otherwise the plugin fetches ppq's "Top Chat
   Models (Last 7 Days)" from its public stats dashboard and pins the top
   few. Cached for an hour; a failed fetch is retried after 10 minutes and
   simply yields fewer pins.
3. **Generic static fallback** — if both layers above yield nothing, a
   small curated list of broadly popular chat models is used. This list is
   deliberately generic and never contains the author's personal picks or
   `private/*` e2e endpoints.

Example — to pin your own preferred models, create the pins file:

```bash
mkdir -p ~/.hermes/ppq-plugin
cat > ~/.hermes/ppq-plugin/pins.json <<'EOF'
[
  "deepseek/deepseek-v4.1-flash",
  "z-ai/glm-5.3-flash",
  "private/your-model-1",
  "private/your-model-2"
]
EOF
```

(Placeholder `private/*` ids stand for e2e endpoints that only exist if your
account has them provisioned; the public ids work for anyone.) Delete the
file to return to the generic behavior.

## Privacy disclosure — read before using

**ppq.ai is a relay, not your model host.** Your `PPQ_API_KEY` and all
prompts (plus tool schemas and responses) are sent to ppq.ai (PayPerQ),
which routes each request to third-party inference partners. Some of those
partners may be outside your jurisdiction, and **frontier providers still
see your prompts**. Privacy protections on this relay are **opt-in per
request**, and this plugin wires two of them:

### Zero Data Retention (opt-in, automatic for badged models)

`GET /v1/models` works unauthenticated and tags every chat model with a
`privacyLevel` (`anon` | `zdr` | `e2e`). For models badged `zdr`, this
plugin automatically sends the request-level opt-in:

```json
{"provider": {"zdr": true, "data_collection": "deny"}}
```

- `zdr: true` routes only to upstream endpoints with a Zero Data Retention
  policy; `data_collection: deny` additionally excludes providers that may
  store data.
- The zdr-capable model set refreshes hourly (1 h TTL) from the unauthenti-
  cated models endpoint; while the refresh is unavailable, a small pinned
  fallback set is used.
- Models badged `anon` or `e2e` are deliberately left untouched —
  requesting `zdr` on a model with no ZDR endpoint fails to route entirely.
- **Caveat:** Claude Opus 4.8 with web search enabled **drops the ZDR
  guarantee**, and some Anthropic/Gemini models may override the provider
  routing fields (`order` / `only` / `ignore`).

### TEE (e2e models — no opt-in flag needed)

The TEE guarantee is intrinsic to the model ID (`privacyLevel: e2e`), not a
request option — there is nothing to opt into. Two paths:

1. **Default enclave router (ppq-hosted):** requests for `e2e` models are
   covered by ppq's TEE enclaves. This is the default. **Honest limits:**
   enclave coverage applies to **chat** by default — image, video and audio
   requests are **not** enclave-covered, and frontier providers still see
   the prompts they serve.
2. **Local `ppq-privacy-verifier` proxy (true E2EE):** for genuinely
   provider-unable-to-read end-to-end encryption, run ppq's local verifier
   proxy instead of the default enclave router.

## Files

- `model-providers/ppq/plugin.yaml` — plugin manifest (`version: 0.3.0`)
- `model-providers/ppq/__init__.py` — provider profile: rate-card cost hook,
  ZDR opt-in routing, `fetch_models`, 3-layer picker pins (user pins file →
  live stats top models → generic static fallback)

The plugin reports cost as **estimated** (from ppq's published rate card),
never `actual` — ppq's API reports no per-response cost of its own.

## License

[MIT](LICENSE)
