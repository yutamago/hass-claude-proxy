# Claude (via proxy) for Home Assistant

A copy of Home Assistant's built-in [Anthropic integration](https://www.home-assistant.io/integrations/anthropic/) that talks to an Anthropic-compatible proxy instead of `api.anthropic.com`.

The built-in integration only asks for an API key and always calls Anthropic's API. This one also asks for a base URL, so you can point it at [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), LiteLLM, a company gateway, or anything else that serves Anthropic's `/v1/messages` and `/v1/models` endpoints.

It has the same conversation agent, AI task entity, Assist tools, prompt caching, thinking and effort settings as the built-in integration. Its domain is `claude_proxy`, so it can run next to the built-in one.

## Requirements

- Home Assistant 2026.11 or newer.
- A proxy that speaks Anthropic's Messages API. Tested with CLIProxyAPI v8.0.73.

## Install

With HACS:

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/yutamago/hass-claude-proxy` with type **Integration**.
3. Install **Claude (via proxy)** and restart Home Assistant.

Without HACS, copy `custom_components/claude_proxy` into your Home Assistant `config/custom_components/` directory and restart.

## Setup

**Settings → Devices & services → Add integration → Claude (via proxy).**

- **Proxy URL** is the proxy's base URL, for example `http://192.168.1.50:8317`. If you paste a URL that ends in `/v1`, the integration strips it.
- **API key** is a key the proxy accepts. For CLIProxyAPI, use one of the entries under `access.api-keys` in its `config.yaml`.

Setup creates one conversation agent and one AI task entity. Pick the conversation agent in **Settings → Voice assistants**, as you would with the built-in integration.

## How it differs from the built-in integration

The config flow, conversation and AI task code is Home Assistant's, unchanged except for the base URL. The one real addition deals with model metadata.

The built-in integration builds its options from Anthropic's Models API. That API returns each model's `capabilities` (which thinking modes, effort levels and server tools it supports) and its `lifecycle` (active, deprecated, retired). Proxies return a bare list. CLIProxyAPI, for example, sends ids, names and token limits, and ignores the `lifecycle` filter. Without that data the options screen would hide the thinking and effort settings, and the AI task entity would have no models to choose from.

So the integration fills in the gaps:

- Missing capabilities come from a bundled table, `model_capabilities.json`. It is generated from the capability data in Home Assistant's own test fixtures, which mirror the Models API. Newer models with the same request shape, such as Claude Fable 5.2, are mapped to an existing entry.
- A missing lifecycle comes from the Anthropic SDK's list of deprecated models. A model not on that list counts as active.
- If the proxy returns real capability or lifecycle data, the integration uses it.

Models the table doesn't know, such as non-Claude models the proxy routes in Anthropic format, still work as conversation agents. They don't get thinking or effort options, and they can't be used for AI tasks, which need structured output.

## Things to know

**Server tools depend on the backend.** Web search, web fetch, code execution and tool search run on Anthropic's servers. They work when the proxy forwards to a real Claude account, and I've checked web search and code execution through CLIProxyAPI. If your proxy sends requests to another provider, leave them off.

**Check Anthropic's terms.** If the proxy pools Claude subscription logins (Pro, Max, Claude Code) rather than API keys, Anthropic's terms may not allow that use, and accounts can be suspended. That's between you and Anthropic. This integration only sends standard API requests to the URL you give it.

**Not affiliated** with Anthropic or Home Assistant.

## Development

The integration is a modified copy of `homeassistant/components/anthropic`. Two branches keep the copy easy to update:

- `upstream` holds Home Assistant's files, renamed to the `claude_proxy` domain and nothing else.
- `main` holds this project's changes on top. `git diff upstream main` shows all of them.

To move to a new Home Assistant release:

```bash
git switch upstream
python3 scripts/sync_upstream.py 2026.12.0     # copies files and records the tag in UPSTREAM
git commit -am "Sync anthropic integration from home-assistant/core 2026.12.0"
git switch main && git merge upstream
.venv/bin/python scripts/gen_capabilities.py   # refresh the capability table from new fixtures
```

Then bump `pytest-homeassistant` and `anthropic` in `requirements_test.txt` to the versions that release uses, and `homeassistant` in `hacs.json` if the code needs the newer release.

Run the tests:

```bash
uv venv --python 3.14 && uv pip install -r requirements_test.txt
.venv/bin/pytest -q -W ignore::DeprecationWarning
```

`scripts/live_smoke.py` runs the integration end to end against a real proxy. It goes through the config flow, has Claude turn on a demo light through Assist, and requests structured data from an AI task. Each step sends a small request.

```bash
CLAUDE_PROXY_URL=http://192.168.1.50:8317 CLAUDE_PROXY_KEY_FILE=~/proxy-key \
  .venv/bin/python scripts/live_smoke.py claude-sonnet-5-5
```

## License

Apache License 2.0, the same as Home Assistant. See `LICENSE` and `NOTICE`.
