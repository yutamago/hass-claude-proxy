# Claude (via proxy) for Home Assistant

A copy of Home Assistant's built-in [Anthropic integration](https://www.home-assistant.io/integrations/anthropic/) that talks to an Anthropic-compatible proxy instead of `api.anthropic.com`.

The built-in integration only asks for an API key and always calls Anthropic's API. This one also asks for a base URL, so you can point it at [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), LiteLLM, a company gateway, or anything else that serves Anthropic's `/v1/messages` and `/v1/models` endpoints.

It has the same conversation agent, AI task entity, Assist tools, prompt caching, thinking and effort settings as the built-in integration. Its domain is `claude_proxy`, so it can run next to the built-in one.

## Requirements

- Home Assistant 2026.11 or newer.
- A proxy that speaks Anthropic's Messages API. Tested with CLIProxyAPI v8.0.73.

## Install

Home Assistant 2026.11 replaced HACS with the built-in Marketplace:

1. Go to **Settings → Marketplace** and select **Add from link**.
2. Enter `yutamago/hass-claude-proxy` and select **Add**. The first time, the Marketplace asks you to connect a GitHub account. If it asks for a type, choose **Integration**.
3. Open **Claude (via proxy)** and install it.

Don't add the repository under **Settings → Apps**. That's the add-on store, and it answers "not a valid app repository".

To install by hand, download `claude_proxy.zip` from the [latest release](https://github.com/yutamago/hass-claude-proxy/releases/latest), unpack it into `config/custom_components/claude_proxy/`, and restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → Claude (via proxy).**

- **Proxy URL** is the proxy's base URL, for example `http://192.168.1.50:8317`. If you paste a URL that ends in `/v1`, the integration strips it.
- **API key** is a key the proxy accepts. For CLIProxyAPI, use one of the entries under `access.api-keys` in its `config.yaml`.

Setup creates one conversation agent and one AI task entity. Pick the conversation agent in **Settings → Voice assistants**, as you would with the built-in integration.

## How it differs from the built-in integration

The config flow, conversation and AI task code is Home Assistant's, unchanged except for the base URL. The one real addition deals with model metadata.

The built-in integration builds its options from Anthropic's Models API. That API returns each model's `capabilities` (which thinking modes, effort levels and server tools it supports) and its `lifecycle` (active, deprecated, retired). Proxies return a bare list. CLIProxyAPI, for example, sends ids, names and token limits, and ignores the `lifecycle` filter. Without that data the options screen would hide the thinking and effort settings, and the AI task entity would have no models to choose from.

So the integration fills in the gaps:

- Missing capabilities come from a table, `model_capabilities.json`. Most entries come from the capability data in Home Assistant's own test fixtures, which mirror the Models API. Models released after the fixtures come from Claude Code's model list. Claude Code knows each current model's effort levels and whether it supports adaptive thinking, and the rest is copied from the previous model in the same family.
- The integration ships with a copy of the table and downloads the latest one from this repository about once a day. It keeps the last good download in Home Assistant's storage, so a restart without internet still has it. New models get their options without waiting for a release.
- A missing lifecycle comes from the Anthropic SDK's list of deprecated models. A model not on that list counts as active.
- If the proxy returns real capability or lifecycle data, the integration uses it.

Models the table doesn't know, such as non-Claude models the proxy routes in Anthropic format, still work as conversation agents. They don't get thinking or effort options, and they can't be used for AI tasks, which need structured output.

## Things to know

**It contacts GitHub once a day.** The table download is a plain GET to `raw.githubusercontent.com`, with no data about your setup. If it fails, the integration keeps using the copy it has.

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
.venv/bin/python scripts/gen_capabilities.py   # rebuild the capability table from the new fixtures
```

Then bump `pytest-homeassistant` and `anthropic` in `requirements_test.txt` to the versions that release uses, and `homeassistant` in `hacs.json` if the code needs the newer release.

### The model table

A daily workflow (`.github/workflows/models.yml`) asks the newest Claude Code for its model list. It starts Claude Code through the Agent SDK and reads the list before sending any prompt, so it needs no login and costs nothing. When the list changes, it rebuilds the table, runs the tests and opens a pull request. Merging it publishes the table to every installed copy within a day.

To do the same by hand:

```bash
cd scripts/claude-code && npm install --no-save @anthropic-ai/claude-agent-sdk@latest && node probe.mjs && cd ../..
.venv/bin/python scripts/gen_capabilities.py
```

The generator prints a warning when Claude Code and Home Assistant's fixtures disagree about a model. The fixtures win, because they mirror Anthropic's API. Claude Code's data only fills in models the fixtures don't have yet.

### Tests


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
