# Goals and rubrics

> Define objectives and acceptance criteria for TalkOps sessions and CI/CD pipelines

Goals and rubrics help TalkOps verify whether its work satisfies the requirements you care about. Use a **goal** for interactive, objective-driven work where the agent should draft acceptance criteria and track progress across turns, and use a **rubric** when you already know the exact grading criteria for interactive or headless CI/CD runs.

## Choose a goal or rubric

Use a **goal** when you have a high-level objective and want TalkOps to break it into verifiable acceptance criteria before starting. Once active, the goal panel tracks each criterion (`[pending]`, `[passed]`, `[failed]`) across turns until the objective is complete.

Use a **rubric** when you already have explicit acceptance criteria—either inline or in a specification file—and want an automated grader loop to evaluate the workspace and feed back remediation steps until every criterion passes.

| Feature | Goals | Rubrics |
| - | - | - |
| **Primary use case** | Interactive multi-turn terminal sessions | Automated quality gates and CI/CD pipelines |
| **Criteria source** | Drafted by the agent from your objective | Explicitly provided by you (inline or `@file`) |
| **Execution loop** | Collaborative tracking with live TUI checklist | Closed-loop worker + grader iteration |
| **Grader model** | Active session model | Configurable via `--rubric-model` + Jev `<200ms` fast-pass |
| **Launch flag / command** | `--goal TEXT` or `/goal <objective>` | `--rubric TEXT\|@PATH` or `/rubric` |

---

## Use a goal

Use `/goal` (or launch with `--goal`) when you know the outcome you want and want TalkOps to draft and track concrete acceptance criteria.

```bash
talkops --goal "Harden production EKS cluster with Pod Security Standards and NetworkPolicies"
```

Or start a goal inside an interactive session:

```text
/goal Implement AWS KMS state encryption and cross-account IAM roles for OpenTofu
```

TalkOps inspects your workspace, generates a checklist of acceptance criteria, and updates progress using its built-in `get_goal` and `update_goal` tools as each milestone is verified:

```text
/goal migrate payment-service deployment to Helm chart with HPA and PDB
start by templating the deployment and service manifests
now add the HorizontalPodAutoscaler and PodDisruptionBudget
run helm lint and template dry-run to verify
```

<p align="center">
  <img src="./images/talkops-goal-rubric-panel.png" alt="TalkOps TUI goal tracking panel showing passed and pending acceptance criteria" width="850" />
</p>

> [!IMPORTANT]
> **Image Placeholder (`docs/talkops-docs/images/talkops-goal-rubric-panel.png`):**
> Capture a screenshot of the `talkops` TUI while an active `/goal` is being tracked, showing:
> 1. The Goal panel above the chat input displaying the objective title.
> 2. Individual acceptance criteria rows with status badges (`[passed]`, `[pending]`, `[failed]`).
> 3. A `update_goal` tool call in the conversation transcript marking a criterion complete.

<details>
<summary><b>Goal command and flag reference</b></summary>

* `--goal TEXT`: Launch an interactive session and initialize a goal from `TEXT`.
* `/goal <objective>`: Draft acceptance criteria from a plain-language objective and track them in the TUI.
* `get_goal` / `update_goal`: Built-in agent tools used to inspect and update criterion status during execution.

</details>

---

## Use a rubric

Use `--rubric` (or `/rubric` in a session) when you already know the acceptance criteria and want an automated grader to verify the agent's changes.

### Specify an inline rubric

Pass criteria directly on the command line for non-interactive tasks:

```bash
talkops -p "Author a Terraform module for an AWS RDS Aurora Postgres cluster" \
  --rubric "1. Multi-AZ deployment is enabled.
2. Storage is encrypted with an AWS KMS customer-managed key.
3. Automated backups are retained for 14 days.
4. Enhanced monitoring and Performance Insights are enabled.
5. Security group restricts port 5432 to the VPC CIDR only." \
  --rubric-model "openai:gpt-4o" \
  --rubric-max-iterations 3 \
  -y
```

### Load a rubric from a file

Prefix a file path with `@` to load reusable architectural or compliance standards from disk:

```bash
talkops -p "Refactor VPC networking" --rubric @specs/vpc-rubric.md -y
```

```markdown
<!-- specs/vpc-rubric.md -->
# VPC Architecture Rubric

1. Primary CIDR block is 10.100.0.0/16 with 3 public and 3 private subnets across 3 AZs.
2. NAT Gateways are deployed redundantly across all 3 availability zones.
3. VPC Flow Logs publish to an encrypted CloudWatch Log Group.
4. Default security group denies all inbound and outbound traffic.
5. VPC gateway endpoints are provisioned for S3 and DynamoDB.
```

---

## How rubric grading works

When a rubric is active, `ReliableRubricMiddleware` runs a closed-loop evaluation cycle after the worker agent finishes a pass:

1. **Worker pass**: The worker agent edits files and runs validation commands in the workspace.
2. **Evidence extraction**: TalkOps collects workspace diffs, file states, and command outputs.
3. **Grader evaluation**:
   * **Tier 1 — Jev System One Fast-Pass (`<200ms`)**: When `--smart` or `TYPESAFE_API_KEY` is active, `JevHybridRubricGrader` & `JevCriteriaCompiler` evaluate all acceptance criteria against the task evidence in parallel in under 200ms. If every criterion passes, the run completes immediately with exit code `0`.
   * **Tier 2 — Frontier LLM diagnostic fallback**: If any criterion fails (or when running without Jev), the configured `--rubric-model` evaluates the failed criteria and generates targeted remediation instructions.
4. **Self-correction**: The remediation report is fed back into the worker agent's context for the next iteration, repeating until all criteria pass or `--rubric-max-iterations` is reached.

> [!TIP]
> Pair your worker agent with a grader model from a different provider family (for example, an Anthropic Claude worker graded by `--rubric-model openai:gpt-4o`) to reduce self-evaluation bias in CI/CD pipelines.

<details>
<summary><b>Rubric CLI flags and CI/CD examples</b></summary>

### Rubric flags

| Flag | Description |
| - | - |
| `--rubric TEXT\|@PATH` | Acceptance criteria as inline text or `@path` to a file |
| `--rubric-model MODEL` | Model used by the rubric grader (`provider:model`). Defaults to the main agent model |
| `--rubric-max-iterations N` | Maximum grader iterations before stopping (default: `3`) |
| `--smart` | Enable Jev System One `<200ms` fast-pass rubric grading |

### CI/CD example: Kubernetes manifest compliance

```bash
talkops -p "Audit and fix deployment.yaml" \
  --rubric "1. Non-root securityContext is enforced.
2. Read-only root filesystem is enabled.
3. Liveness and readiness probes have timeout thresholds.
4. CPU and memory requests and limits are defined." \
  --rubric-model "openai:gpt-4o" \
  --rubric-max-iterations 3 \
  --quiet \
  -y
```

### CI/CD example: Automated Terraform refactoring

```bash
talkops -p "Refactor database module to support read replicas" \
  --rubric @specs/db-replica-rubric.md \
  --rubric-model "anthropic:claude-3-7-sonnet-20250219" \
  --max-turns 15 \
  --quiet \
  -y
```

</details>

---

## See also

* [TalkOps overview](./overview.md)
* [Quickstart](./quickstart.md)
* [Command reference](./cli-reference.md)
* [Approval modes](./approval-mode.md)
