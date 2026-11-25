// Ask Claude Code which models it knows and write scripts/claude_code_models.json.
//
// Starts Claude Code through the Agent SDK (which bundles the binary), reads
// supportedModels() and exits before any prompt is sent. It needs no login and
// sends no request to a model.
//
//   cd scripts/claude-code && npm install --no-save @anthropic-ai/claude-agent-sdk@latest
//   node probe.mjs

import { query } from "@anthropic-ai/claude-agent-sdk";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const OUT = new URL("../claude_code_models.json", import.meta.url);
const sdkVersion = JSON.parse(
  readFileSync(
    new URL("node_modules/@anthropic-ai/claude-agent-sdk/package.json", import.meta.url),
  ),
).version;

async function* noPrompt() {
  await new Promise(() => {});
}

const cwd = mkdtempSync(join(tmpdir(), "claude-code-probe-"));
const q = query({ prompt: noPrompt(), options: { cwd, settingSources: [] } });
const timer = setTimeout(() => {
  console.error("Claude Code did not report its models within 60 s");
  process.exit(1);
}, 60_000);
const reported = await q.supportedModels();
clearTimeout(timer);
q.close();

const models = new Map();
for (const info of reported) {
  // `default` is Claude Code's pointer at its default model; the alias rows name it too.
  if (info.value === "default") continue;
  const id = (info.resolvedModel ?? info.value).replace(/\[[^\]]*\]$/, "").trim();
  // Several aliases can resolve to one id; keep one row per id.
  if (!id.startsWith("claude-") || models.has(id)) continue;
  models.set(id, {
    id,
    display_name: info.displayName,
    effort: info.supportsEffort === false ? [] : (info.supportedEffortLevels ?? []),
    adaptive: Boolean(info.supportsAdaptiveThinking),
  });
}
if (models.size === 0) {
  console.error("Claude Code reported no models");
  process.exit(1);
}

const sorted = [...models.values()].sort((a, b) => a.id.localeCompare(b.id));
writeFileSync(
  OUT,
  JSON.stringify({ agent_sdk_version: sdkVersion, models: sorted }, null, 2) + "\n",
);
console.log(`Claude Code (Agent SDK ${sdkVersion}) reports ${sorted.map((m) => m.id).join(", ")}`);
process.exit(0);
