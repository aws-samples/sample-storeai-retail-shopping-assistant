// StoreAI v2 config builder — emits the same shape as deploy/config/defaults.json
// Module hard-deps mirror deploy/config/registry.yaml (keep in sync).
const DEPS = {
  "data-plane": [], "ecr": [], "eks": [],
  "mcp-tools": ["data-plane"],
  "litellm-gateway": ["eks", "ecr"],
  "orchestrator": ["eks", "ecr", "mcp-tools", "data-plane", "litellm-gateway"],
  "voice-nova": ["eks", "ecr"],
  "avatar-heygen": ["eks", "ecr"],
  "whisper": ["eks", "ecr", "litellm-gateway"],
  "tts": ["eks", "ecr", "litellm-gateway"],
  "llm": ["eks", "ecr", "litellm-gateway"],
  "image-edit-model": ["eks", "ecr", "litellm-gateway"],
  "fashn-model": ["eks", "ecr", "litellm-gateway"],
  "vton": ["data-plane", "mcp-tools", "litellm-gateway"],
  "cdn": ["data-plane"], "frontend": ["cdn"],
  "seed-data": ["data-plane"], "cognito-user": ["data-plane"],
};
const LAYER = {
  "data-plane": "infra", "ecr": "infra", "eks": "infra",
  "mcp-tools": "L2", "vton": "L2", "orchestrator": "L2", "voice-nova": "L2", "avatar-heygen": "L2",
  "litellm-gateway": "L3",
  "llm": "L1", "image-edit-model": "L1", "fashn-model": "L1", "whisper": "L1", "tts": "L1",
  "cdn": "edge", "frontend": "edge", "seed-data": "ops", "cognito-user": "ops",
};
// Default enabled set (mirrors defaults.json)
const DEFAULT_ON = ["data-plane","ecr","eks","mcp-tools","litellm-gateway","orchestrator","voice-nova","cdn","frontend","seed-data","cognito-user"];
const ORDER = ["data-plane","ecr","eks","mcp-tools","litellm-gateway","orchestrator","voice-nova","whisper","avatar-heygen","vton","image-edit-model","fashn-model","llm","tts","cdn","frontend","seed-data","cognito-user"];

const $ = (id) => document.getElementById(id);

function renderModules() {
  const box = $("modules");
  box.innerHTML = "";
  ORDER.forEach((m) => {
    const row = document.createElement("label");
    row.className = "mod"; row.dataset.mod = m;
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.m = m; cb.checked = DEFAULT_ON.includes(m);
    const nameSpan = document.createElement("span"); nameSpan.textContent = m;
    const layerSpan = document.createElement("span"); layerSpan.className = "layer"; layerSpan.textContent = LAYER[m];
    row.append(cb, nameSpan, layerSpan);
    box.appendChild(row);
  });
  box.querySelectorAll("input").forEach((cb) => cb.addEventListener("change", refreshDeps));
  refreshDeps();
}

function selectedModules() {
  return [...document.querySelectorAll('#modules input:checked')].map((c) => c.dataset.m);
}

function closure(sel) {
  const set = new Set(sel), stack = [...sel];
  while (stack.length) {
    const m = stack.pop();
    (DEPS[m] || []).forEach((d) => { if (!set.has(d)) { set.add(d); stack.push(d); } });
  }
  return set;
}

function refreshDeps() {
  const sel = selectedModules();
  const full = closure(sel);
  const auto = [...full].filter((m) => !sel.includes(m));
  document.querySelectorAll("#modules .mod").forEach((row) => {
    row.classList.toggle("auto", auto.includes(row.dataset.mod) && !selectedModules().includes(row.dataset.mod));
  });
  $("depNote").textContent = auto.length
    ? `Auto-enabled dependencies: ${auto.join(", ")}`
    : "No extra dependencies needed.";
}

function buildConfig() {
  const enabledSet = closure(selectedModules());
  const modules = {};
  ORDER.forEach((m) => {
    modules[m] = { enabled: enabledSet.has(m) };
    // 1 replica: conversation state is per-process (see docs/known-limitations.md).
    if (m === "orchestrator") modules[m].replicas = 1;
    if (m === "cdn") modules[m].demoMode = "booth";
  });
  const en = (m) => enabledSet.has(m);
  return {
    version: "1",
    global: {
      env: $("env").value, region: $("region").value.trim(),
      accountId: "auto",
      tfStateBucket: $("tfStateBucket").value.trim(),
      stateBucketRegion: $("stateBucketRegion").value.trim(),
      stateKeyPrefix: $("stateKeyPrefix").value.trim(),
    },
    prerequisites: {
      capacityBlock: { reservationId: $("cbReservationId").value.trim(), instanceType: $("cbInstanceType").value.trim() || "trn2.48xlarge", az: $("cbAz").value.trim(), nodeCount: 1 },
      models: { llmS3Path: $("llmS3Path").value.trim(), vtonS3Path: $("vtonS3Path").value.trim(), compileIfMissing: true },
      avatar: { provider: "heygen", apiKey: $("heygenApiKey").value.trim() },
      dns: { customDomain: $("customDomain").value.trim(), hostedZoneId: $("hostedZoneId").value.trim() },
      auth: { adminEmail: $("adminEmail").value.trim() },
    },
    engines: {
      llm: { default: "bedrock-claude", backends: {
        "bedrock-claude": { enabled: true, provider: "bedrock", model: "us.anthropic.claude-sonnet-4-6" },
        "qwen3-neuron": { enabled: en("llm"), provider: "vllm", url: "http://storeai-llm:8080" } } },
      vton: { default: "auto", engines: {
        "image-edit-model": { enabled: en("image-edit-model"), kind: "neuron" },
        "fashn-model": { enabled: en("fashn-model"), kind: "gpu" } } },
    },
    modules,
  };
}

function currentJSON() { return JSON.stringify(buildConfig(), null, 2); }

function flash(msg) { const s = $("status"); s.textContent = msg; setTimeout(() => (s.textContent = ""), 2500); }

$("genBtn").addEventListener("click", () => { $("output").value = currentJSON(); flash("Generated."); });
$("dlBtn").addEventListener("click", () => {
  const blob = new Blob([$("output").value || currentJSON()], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "storeai.config.json"; a.click();
  flash("Downloaded storeai.config.json");
});
$("cpBtn").addEventListener("click", async () => { await navigator.clipboard.writeText($("output").value || currentJSON()); flash("Config copied."); });
$("cpCmdBtn").addEventListener("click", async () => {
  await navigator.clipboard.writeText("deploy/storeai up --config storeai.config.json");
  flash("Command copied.");
});

["input", "change"].forEach((ev) => document.addEventListener(ev, (e) => {
  if (e.target.closest("main")) $("output").value = currentJSON();
}));

renderModules();
$("output").value = currentJSON();
