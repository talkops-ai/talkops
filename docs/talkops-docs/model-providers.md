# Model Providers & Smart Mode Routing

TalkOps supports 20+ LLM providers through LangChain's unified `init_chat_model` interface and pairs them with the **Dynamic Model Pool (`DynamicModelPoolManager`)** so **Jev Smart Mode** can route every prompt across 3 cost/intelligence tiers (`fast`, `standard`, `powerful`) in `<70ms`.

> [!NOTE]
> 📸 **Image Placeholder — `docs/assets/model-selector-and-pool.png`**
> **What to capture:** Side-by-side screenshot of the `/model` interactive selector modal and the `/pool` modal showing the active Tier 0 (`fast`, `effort="off"`), Tier 1 (`standard`, `effort="medium"`), and Tier 2 (`powerful`, `effort="max"`) models.

---

## Supported model providers

Set the corresponding API key via `talkops auth set <provider> <key>` or environment variable to activate any provider:

| Provider | Prefix | Default / Recommended Models | Credential Env Var / Auth Key |
| :--- | :--- | :--- | :--- |
| **Anthropic** | `anthropic:` | `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5` | `ANTHROPIC_API_KEY` (`talkops auth set anthropic`) |
| **OpenAI** | `openai:` | `gpt-5.4`, `o3-pro`, `o3`, `gpt-5.4-mini` | `OPENAI_API_KEY` (`talkops auth set openai`) |
| **Google Gemini** | `google_genai:` *(alias `google:`)* | `gemini-3.1-pro`, `gemini-3-flash`, `gemini-3.1-flash-lite` | `GOOGLE_API_KEY` / `GEMINI_API_KEY` (`talkops auth set google_genai`) |
| **AWS Bedrock** | `bedrock:` | `us.anthropic.claude-sonnet-4-6-v1:0`, `us.anthropic.claude-haiku-4-5-v1:0` | Native AWS chain (`AWS_PROFILE`, `AWS_REGION`, IAM Role, `AWS_BEARER_TOKEN_BEDROCK`) |
| **Azure OpenAI** | `azure_openai:` *(alias `azure:`)* | `gpt-5.4`, `o3` | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` |
| **Google Vertex AI (Gemini)** | `google_vertexai:` *(alias `vertexai:`)* | `gemini-3.1-pro`, `gemini-2.5-pro` | `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT_ID` |
| **Google Vertex AI (Anthropic)** | `google_anthropic_vertex:` | `claude-sonnet-5@20250929`, `claude-opus-5@20251101` | `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT_ID` |
| **OpenRouter** | `openrouter:` | Any hosted model (`anthropic/claude-sonnet-4-6`, etc.) | `OPENROUTER_API_KEY` (`talkops auth set openrouter`) |
| **DeepSeek** | `deepseek:` | `deepseek-reasoner`, `deepseek-chat` | `DEEPSEEK_API_KEY` (`talkops auth set deepseek`) |
| **Groq** | `groq:` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` (`talkops auth set groq`) |
| **Fireworks AI** | `fireworks:` | `accounts/fireworks/models/deepseek-v3` | `FIREWORKS_API_KEY` (`talkops auth set fireworks`) |
| **Together AI** | `together:` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | `TOGETHER_API_KEY` (`talkops auth set together`) |
| **Mistral AI** | `mistralai:` *(alias `mistral:`)* | `mistral-large-latest`, `codestral-latest` | `MISTRAL_API_KEY` (`talkops auth set mistralai`) |
| **xAI (Grok)** | `xai:` | `grok-3`, `grok-3-mini` | `XAI_API_KEY` (`talkops auth set xai`) |
| **Cerebras / SambaNova / Cohere / Perplexity** | `cerebras:`, `sambanova:`, `cohere:`, `perplexity:` | Provider-specific high-throughput models | `CEREBRAS_API_KEY`, `SAMBANOVA_API_KEY`, `COHERE_API_KEY`, `PPLX_API_KEY` / `PERPLEXITY_API_KEY` |
| **NVIDIA / Hugging Face / IBM watsonx / Baseten / LiteLLM** | `nvidia:`, `huggingface:`, `ibm:`, `baseten:`, `litellm:` | Enterprise & open-weight hosted models | `NVIDIA_API_KEY`, `HUGGINGFACEHUB_API_TOKEN` / `HF_TOKEN`, `WATSONX_APIKEY`, `BASETEN_API_KEY`, `LITELLM_API_KEY` |
| **Ollama (Local / Remote)** | `ollama:` | `qwen2.5-coder:32b`, `llama3.3:70b` | `OLLAMA_HOST` / `OLLAMA_BASE_URL` (default `http://localhost:11434`), optional `OLLAMA_API_KEY` |

---

## How Smart Mode 3-tier model & reasoning routing works

When you run TalkOps in **Smart Mode** (`talkops --smart` or `/smart`), you don't pay frontier model prices or wait for chain-of-thought thinking tokens on simple prompts.

Implemented in `src/talkops/model/pool.py` (`DynamicModelPoolManager`) and `src/talkops/middleware/jev_model_router.py` (`JevDynamicModelRouterMiddleware`), TalkOps maintains a 3-slot model pool (**`fast`**, **`standard`**, **`powerful`**) and routes each turn in `<70ms`:

### 1. Tier assignment & automatic reasoning effort adjustment

| Tier Slot | Default Auto-Populated Models | Reasoning Effort (`_resolve_dynamic_effort`) | When Jev Routes Here |
| :--- | :--- | :--- | :--- |
| **Tier 0: `fast`** | `claude-haiku-4-5`, `gpt-5.4-mini`, `gemini-3.1-flash-lite` | **`off`** *(reasoning disabled)* | Greetings, conversational follow-ups, single-file reads, status checks (`kubectl get pods`, `git status`). **Reduces token cost by 80–90% and eliminates thinking latency.** |
| **Tier 1: `standard`** | `claude-sonnet-4-6`, `gpt-5.4`, `gemini-3-flash` | **`medium`** *(or `"low"` for lightweight models)* | Focused single-domain DevOps tasks: writing a Dockerfile, editing a Terraform module, or inspecting service logs. |
| **Tier 2: `powerful`** | `claude-opus-4-6`, `o3-pro`, `gemini-3.1-pro` | **`max`** *(Anthropic)* / **`xhigh`** *(OpenAI)* / **`high`** | Multi-agent delegation, SRE incident root-cause correlation (`CrashLoopBackOff`, `OOMKilled`), FinOps cost optimization, security/IAM audits, and cross-service refactoring. |

### 2. Orchestrator vs. subagent routing criteria

`JevDynamicModelRouterMiddleware` passes `routing_state["agent_role"]` (`orchestrator` vs. `subagent`) into Jev:
- **Orchestrator (`ORCHESTRATOR_ROUTE_CRITERIA`)**: Evaluates whether the overall user request requires multi-agent synthesis (`powerful`), single-domain execution (`standard`), or reflex lookup (`fast`).
- **Subagents (`SUBAGENT_ROUTE_CRITERIA`)**: Evaluates the narrow delegated sub-task independently. For example, while the orchestrator uses `claude-opus-4-6` (`effort="max"`) to correlate an outage, a subagent dispatched solely to list recent Git commits or fetch a pod log can run on `fast` or `standard`, multiplying cost savings across parallel subagent fan-outs.

### 3. Confidence safeguards & automatic baseline restoration

- **Confidence Safeguards (`_apply_confidence_safeguards`)**: If router confidence is `< 0.55` on prompts containing high-complexity operational markers (`optimi`, `cost`, `finop`, `crashloop`, `oomkill`, `incident`, `outage`, `architect`, `refactor`, `secur`), TalkOps automatically escalates the turn to **`powerful`** (`standard` for subagents). If a `fast` classification has confidence `< 0.55`, it promotes to **`standard`**.
- **Baseline Restoration on `/manual` or `/auto`**: When you switch from `/smart` back to `/manual` or `/auto`, TalkOps automatically restores your baseline model (`/model`) and clears any sticky checkpoint route.

---

## Configuring your model and Smart Mode pool

### Method 1: CLI flags (`-M` / `--model`)

Pass a single model or a comma-separated 3-tier pool (`fast,standard,powerful`) at startup:

```bash
# Pin a single baseline model
talkops -M anthropic:claude-sonnet-4-6

# Define a custom 3-tier pool and launch in Smart Mode
talkops --smart -M "anthropic:claude-haiku-4-5,anthropic:claude-sonnet-4-6,anthropic:claude-opus-4-6"
```

### Method 2: Interactive slash commands (`/model`, `/pool`, `/effort`, `/fast`)

Inside the TUI:

```text
/model anthropic:claude-sonnet-4-6
/pool anthropic:claude-haiku-4-5,anthropic:claude-sonnet-4-6,anthropic:claude-opus-4-6
/effort high
/fast
```

### Method 3: `config.toml`

Persist your default model, 3-tier Smart Mode pool, and custom OpenAI-compatible endpoints in `~/.talkops/config.toml` or `.talkops/config.toml`:

```toml
[models]
default = "anthropic:claude-sonnet-4-6"
reasoning_effort = "medium"
pool = [
  "anthropic:claude-haiku-4-5",
  "anthropic:claude-sonnet-4-6",
  "anthropic:claude-opus-4-6"
]

# Optional: Mix providers across tiers (e.g., Gemini Flash-Lite for Tier 0, Claude Opus for Tier 2)
# pool = [
#   "google_genai:gemini-3.1-flash-lite",
#   "anthropic:claude-sonnet-4-6",
#   "anthropic:claude-opus-4-6"
# ]
```

---

## Next steps

- **[Approval Modes & Jev Smart Mode](approval-mode.md)** — Deep dive into Jev routing criteria and `<100ms` System One tool safety classification
- **[Credentials & Authentication](credentials.md)** — Store encrypted provider keys via `talkops auth set`
- **[`config.toml` Reference](config.toml.md)** — View all `[models]` configuration keys
