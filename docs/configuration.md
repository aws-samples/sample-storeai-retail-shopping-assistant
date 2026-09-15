# Configuration

StoreAI is driven by a single file, `storeai.config.json`. This document describes every setting.
The authoritative JSON Schema lives at `deploy/config/schema.json`; sensible defaults are in
`deploy/config/defaults.json`.

## Producing the file

Three methods produce an identical file — use whichever you prefer:

```bash
./deploy/storeai config init                 # defaults, non-interactive
./deploy/storeai config init --interactive   # guided prompts
```

Or open `deploy/ui/index.html` in a browser for a visual builder with live dependency resolution.
Validate any config against the schema with:

```bash
./deploy/storeai config validate storeai.config.json
```

Alternatively, copy the committed template and edit it by hand:

```bash
cp storeai.config.example.json storeai.config.json
```

`storeai.config.example.json` is a sanitized template kept in version control; `storeai.config.json`
holds your real values and is **git-ignored** (so account IDs, domains, capacity-block IDs, and
credentials never get committed). The deployer defaults to `deploy/config/defaults.json`, so point it
at your file with `--config storeai.config.json` or by exporting `STOREAI_CONFIG=storeai.config.json`.

## Prerequisites

Everything is configurable, and almost everything is optional with a default. The table below groups
inputs by when they matter.

| Setting | Required? | Default | Needed by |
|---------|-----------|---------|-----------|
| AWS credentials / profile | Yes | environment/default profile | Everything |
| `global.tfStateBucket` | **Yes** | — | Terraform backend (user-provided; never created or deleted by the deployer) |
| `global.region` | No | `us-east-2` | Everything |
| `global.env` | No | `dev` | Everything (`dev` or `prod`) |
| `global.accountId` | No | auto (from STS) | Everything |
| `capacityBlock.reservationId` | Only for Neuron models | `""` (skip) | Self-hosted LLM / Qwen Image-Edit |
| `capacityBlock.instanceType` | No | `trn2.48xlarge` | Neuron models |
| `models.llmS3Path` / `models.vtonS3Path` | No | `""` (compile if empty) | Self-hosted models |
| `avatar.apiKey` (HeyGen) | No | `""` (avatar off) | `avatar-heygen` |
| `huggingface.token` | No | `""` (public downloads) | Faster **authenticated** HuggingFace model downloads for self-hosted models; falls back to public if empty |
| `dns.customDomain` + `dns.hostedZoneId` | No | `""` (default CloudFront URL) | `cdn` — serves the app at `storeai.<customDomain>` |
| `auth.adminEmail` | No | `""` (→ `admin@storeai.local`) | Admin sign-in user. A real email gets an emailed Cognito invite (set your own password on first sign-in); blank creates `admin@storeai.local` with **no password** (set it via the CLI command the deployer prints). No password is ever stored in config. |

> The Terraform state bucket is the **only** infrastructure prerequisite. Every other AWS resource —
> including the compiled-model S3 bucket and all SSM parameters — is created and managed by Terraform.
> See [design-decisions.md](design-decisions.md) (D-013).

> **`customDomain` — serving the app at your own domain.** When `global.customDomain` (+
> `hostedZoneId`) is set, the deployer serves the app at **`storeai.<customDomain>`** — a dedicated
> subdomain, never the bare domain (which you may already use for other content). It automatically
> provisions a **us-east-1 ACM certificate**, adds the **CloudFront alias**, and creates the **Route53
> CNAME**, and the try-on **QR** share links use that URL. Leave `customDomain` **empty** to serve on
> the default `*.cloudfront.net` URL instead — everything, including the QR, still works. A freshly
> provisioned custom domain can take ~10–20 minutes to propagate. See
> [troubleshooting.md](troubleshooting.md#qr--share-link-is-not-reachable).

## Minimum to deploy

The state bucket is the only required value; `region` and `env` have defaults and are shown here only
for clarity. This is the smallest config that deploys:

```json
{
  "global": {
    "tfStateBucket": "your-terraform-state-bucket",
    "region": "us-east-2",
    "env": "dev"
  }
}
```

## File shape

```jsonc
{
  "version": "1",
  "global": {
    "env": "dev",
    "region": "us-east-2",
    "accountId": "auto",
    "tfStateBucket": "your-terraform-state-bucket",
    "stateBucketRegion": "us-east-1",           // backend region may differ from provider region (D-024)
    "stateKeyPrefix": "retail-shopping-agent/storeai"
  },
  "prerequisites": {
    "capacityBlock": { "reservationId": "", "instanceType": "trn2.48xlarge", "az": "", "nodeCount": 1 },
    "models":        { "llmS3Path": "", "vtonS3Path": "", "compileIfMissing": true },
    "avatar":        { "provider": "heygen", "apiKey": "" },
    "dns":           { "customDomain": "", "hostedZoneId": "" },
    "auth":          { "adminEmail": "" }
  },
  "engines": {
    "llm": {
      "default": "bedrock-claude",
      "backends": {
        "bedrock-claude": { "enabled": true,  "provider": "bedrock", "model": "us.anthropic.claude-sonnet-4-6" },
        "qwen3-neuron":   { "enabled": false, "provider": "vllm",    "url": "http://storeai-llm:8080" }
      }
    },
    "vton": {
      "default": "auto",                          // prefer Qwen Image-Edit if available, else the single engine
      "engines": {
        "fashn":           { "enabled": true,  "kind": "gpu",    "url": "http://storeai-fashn-vton:8081" },
        "qwen_image_edit": { "enabled": false, "kind": "neuron" }
      }
    }
  },
  "modules": {
    "data-plane":      { "enabled": true },
    "ecr":             { "enabled": true },
    "network":         { "enabled": true },
    "eks":             { "enabled": true },
    "mcp-tools":       { "enabled": true },
    "litellm-gateway": { "enabled": true },
    "orchestrator":    { "enabled": true, "replicas": 2 },
    "vton":            { "enabled": true },
    "voice-nova":      { "enabled": true },
    "whisper":         { "enabled": false },
    "avatar-heygen":   { "enabled": false },
    "fashn-model":     { "enabled": true },
    "image-edit-model":{ "enabled": false },
    "llm":             { "enabled": false },
    "cdn":             { "enabled": true, "demoMode": "booth" },
    "frontend":        { "enabled": true },
    "seed-data":       { "enabled": true },
    "cognito-user":    { "enabled": true }
  }
}
```

## Engines: adding or swapping models

The `engines` block is the pluggability point. Because every model call flows through the LiteLLM
gateway, adding a model is a configuration change:

- **Add an LLM** — add an entry under `engines.llm.backends` and enable it. Set `engines.llm.default`
  to route chat to it. The `CHAT_MODEL` environment variable, when set on the orchestrator, overrides
  the `engines.llm.default` config-file value.
- **Add a try-on engine** — add an entry under `engines.vton.engines`. `default: "auto"` prefers
  Qwen Image-Edit when available and otherwise uses the single enabled engine.

No frontend or orchestrator code changes are needed; available models are sourced from this registry
and surfaced in the UI, and each interaction's cost is shown with its model or engine and added to the
session total (see D-014, D-015).

## Modules and dependency resolution

Set `modules.<name>.enabled` to include a module. The resolver then:

- **Auto-enables hard dependencies** and prints a warning (for example, enabling `orchestrator`
  brings up `eks`, `ecr`, `mcp-tools`, `data-plane`, and `litellm-gateway`).
- **Fails fast** if an enabled module is missing a required prerequisite — for example,
  `avatar-heygen` with an empty API key, or a Neuron model with no capacity block — before making any
  AWS changes.

## CLI reference

```
storeai up       [--config FILE] [--module NAME]... [--env ENV] [--region R] [--yes]
storeai down     [--config FILE] [--module NAME]... [--all]
storeai plan     [--config FILE]
storeai status   [--config FILE]
storeai config   init [--interactive] | validate [FILE]
```

- `--module` on `up` deploys a module plus its resolved dependencies, so the module has what it
  needs to run.
- `--module` on `down` removes exactly the named module and nothing else. Dependencies are left in
  place, since they are shared, and modules that depend on the one being removed are reported as
  warnings rather than removed with it. Use `--all` for a full teardown.
- `--yes` runs unattended (no prompts; defaults fill any gaps).
- `up` always runs preflight first: required tools, valid credentials, container runtime, and a
  config whose enabled modules have satisfied prerequisites.
