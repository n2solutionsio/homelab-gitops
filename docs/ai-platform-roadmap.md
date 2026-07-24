# AI Platform Engineering Roadmap

**Status**: Active
**Owner**: Nick Narcise
**Purpose**: Build a secured, observable, evaluated, self-hostable **agentic-AI platform** on the homelab k3s cluster — as a working reference implementation and a **Forward Deployed Engineer (FDE) skill-building portfolio**. Each track is implemented for real *and* written up (the write-ups are the [blog series](#blog-series-map)).

> This is a living document. It is the tracking **epic** and the **architecture roadmap** in one. Sub-work ships one layer at a time, each as its own PR.

---

## Why this exists

The FDE role at an AI-infra company means embedding with customers to **stand up, integrate, evaluate, and secure AI infrastructure** in their environment. This platform is a proving ground for exactly that toolkit. The deliverable is not just "it runs" — it's being able to **articulate the tradeoffs** ("why SPIRE vs Cilium mesh-auth", "why Langfuse vs Phoenix", "when a customer needs OPA") the way an FDE must in the field.

Two principles run through every track:
1. **Implement it for real** — no toy demos; production-shaped configs, GitOps-managed, observable.
2. **Explain the choice** — every track lists the *tradeoffs to articulate*. That framing is the actual skill.

---

## Current-state snapshot (grounded, 2026-07-24)

| Domain | Today | Gap |
|---|---|---|
| **Agent runtime** | kagent 0.9.8 (chart), kmcp 0.3.0, agentgateway 0.9.0 fronting 5 MCP servers | on latest kmcp; no LLM-layer gateway |
| **LLM calls** | kagent controller → Anthropic **directly** | no gateway = no routing/guardrails/central token metrics |
| **Traces** | 🔴 none — kagent controller has **no OTel export** | biggest single unlock |
| **Metrics** | 🟡 agentgateway `/metrics` **live on :15020 but unscraped**; ExternalSecret + node/pod alerts shipped | no MCP/agent dashboards |
| **Logs** | 🟡 stdout → Loki works; not structured/correlated | no trace-linked logs |
| **Evaluation** | 🔴 none | no eval harness, no CI evals, no red-team |
| **Identity (AuthN)** | 🔴 `kagent AUTH_MODE=unsecure` (trusts `X-User-Id`); no SSO/IdP; ArgoCD dex unconfigured | impersonation gap |
| **AuthZ / policy** | 🔴 none (no OPA/Kyverno/Gatekeeper) | no admission or agent-level authz |
| **Workload mTLS** | 🟡 Cilium (CRD identity, L7 proxy) but **no encryption/mutual-auth enabled** | no zero-trust data plane |
| **Secrets / PKI** | 🟢 OpenBao + ExternalSecrets + cert-manager; identity model documented | 1Password→OpenBao seeding still manual |
| **Supply chain** | 🟡 Trivy scanning (alerts firing), Falco runtime | no image signing / SBOM gate |

Foundational security thinking already exists in [`agent-identity-security-model.md`](./agent-identity-security-model.md) — the identity track below **extends** it.

---

## Target architecture

```
                          ┌─────────────────────────── Humans ───────────────────────────┐
                          │  SSO (OIDC) via Keycloak → Grafana / ArgoCD / kagent-ui / n8n │
                          └───────────────────────────────┬──────────────────────────────┘
                                                           │ OIDC
   ┌───────────────────────────────────────────────────────────────────────────────────────┐
   │  CONTROL / POLICY PLANE                                                                  │
   │   Keycloak (AuthN, OIDC issuer)   ·   OPA (AuthZ decisions)   ·   OpenBao (secrets/PKI)  │
   └───────────────────────────────────────────────────────────────────────────────────────┘
                                                           │
   ┌───────────────────────────────────────────────────────────────────────────────────────┐
   │  AGENT / AI DATA PLANE                                                                   │
   │                                                                                         │
   │   kagent controller ──► AI Gateway (agentgateway/kgateway) ──► LLM providers            │
   │        │                    │  routing · guardrails · token/cost · rate-limit           │
   │        │                    └──► OPA ext-authz (who may call which model/tool)          │
   │        ▼                                                                                 │
   │   agents ──► agentgateway (MCP proxy) ──► MCP tool servers (argocd/grafana/pg/...)       │
   │                                                                                         │
   │   mTLS everywhere: SPIFFE/SPIRE identities  ⟷  Cilium WireGuard + mutual auth            │
   └───────────────────────────────────────────────────────────────────────────────────────┘
                                                           │  OTLP (traces/metrics/logs)
   ┌───────────────────────────────────────────────────────────────────────────────────────┐
   │  OBSERVABILITY + EVALUATION PLANE                                                        │
   │   Alloy → Tempo (traces) · Prometheus (metrics) · Loki (logs) · Grafana (dashboards)     │
   │   LLM-obs & eval: Langfuse / Phoenix  ·  CI evals: promptfoo / DeepEval  ·  red-team     │
   └───────────────────────────────────────────────────────────────────────────────────────┘
```

---

## The tracks (epic breakdown)

Each track is an independent workstream. Order of *value delivery* is in [Sequencing](#sequencing--milestones); tracks otherwise run in parallel.

### Track A — Observability & Telemetry
**Goal:** every prompt, tool call, token, and request is traced, measured, and logged into the existing Grafana/Tempo/Loki/Prometheus stack.

- **Current:** agentgateway metrics live but unscraped; controller not exporting OTel; logs unstructured.
- **Target:** kagent OTel export → Tempo; agentgateway metrics scraped (PodMonitor :15020); MCP/agent Grafana dashboards; trace-correlated logs.
- **Components:** kagent OTel config (Helm), PodMonitor, Grafana dashboards, Alloy pipelines.
- **Tradeoffs to articulate:** OTLP gRPC vs HTTP; sampling strategy (dev all-on vs prod ratio); metrics cardinality (per-tool labels) vs cost; push (OTLP) vs pull (Prometheus).
- **Tests/evals:** assert spans arrive in Tempo for a known agent run; dashboard panels resolve; alert on telemetry-absent (like the ESO `absent()` guard already shipped).
- **Blog:** *"Wiring OTel through an agentic platform: from `config: {}` to full traces."*

### Track B — Testing & Evaluation  *(first-class — see [dedicated strategy](#testing--evaluation-strategy))*
**Goal:** continuously test and evaluate models, agents, performance, cost, and safety — not just observe them.

- **Current:** none.
- **Target:** eval platform (Langfuse or Phoenix) ingesting OTel; offline eval suites (promptfoo/DeepEval) in CI; load/perf harness; security red-team suite; regression gates on PRs.
- **Tradeoffs to articulate:** observability (what happened) vs evaluation (how good); LLM-as-judge vs deterministic asserts vs human review; offline eval vs online/production eval; Langfuse vs Phoenix vs Braintrust.
- **Blog:** whole sub-series (below).

### Track C — AI Gateway, Routing & Guardrails
**Goal:** a controllable, observable choke point for LLM + agent traffic.

- **Current:** kagent → Anthropic direct; agentgateway only proxies MCP tools.
- **Target:** agentgateway/kgateway AI-gateway in front of LLM providers — multi-provider & content/model-based routing, weighted/canary routing (enables model A/B for evals), rate limiting, cost caps, prompt guardrails, semantic caching.
- **Components:** kgateway or agentgateway LLM backends, Gateway API resources, guardrail policies.
- **Tradeoffs to articulate:** in-app SDK routing vs gateway routing; where guardrails belong (gateway vs agent); caching correctness vs freshness; kgateway (full Gateway-API) vs agentgateway standalone.
- **Tests/evals:** canary-route a % to a second model and compare eval scores/cost/latency; guardrail catches a prompt-injection payload; failover on provider 5xx.
- **Blog:** *"Putting an AI gateway in front of your agents: routing, guardrails, and cost control."*

### Track D — Identity & Access (AuthN)  *(extends [agent-identity-security-model.md](./agent-identity-security-model.md))*
**Goal:** kill trusted-header impersonation; every human and workload has a real, attributable identity.

- **Current:** `kagent AUTH_MODE=unsecure`; no IdP; UIs use standalone admin logins.
- **Target:** Keycloak as OIDC issuer → SSO for Grafana/ArgoCD/kagent-ui/n8n; OIDC-based agent/service auth replacing `X-User-Id`.
- **Components:** Keycloak, OIDC clients per UI, kagent secure auth mode.
- **Tradeoffs to articulate:** Keycloak vs Authentik vs Dex; OIDC vs mTLS-only service identity; token lifetime vs revocation; where to terminate auth (gateway vs app).
- **Tests/evals:** unauthenticated A2A call is rejected; SSO login flow for each UI; token expiry/refresh behaves.
- **Blog:** *"From `AUTH_MODE=unsecure` to OIDC: giving AI agents real identities."*

### Track E — Authorization & Policy
**Goal:** given an identity, enforce what it may do — at admission and at the agent/tool call.

- **Current:** none.
- **Target:** OPA for (a) k8s admission policy and (b) agentgateway **external authz** — "which identity may invoke which MCP tool / model."
- **Components:** OPA/Gatekeeper (or Kyverno for admission), Rego policies, agentgateway ext-authz hook.
- **Tradeoffs to articulate:** OPA/Rego vs Kyverno (YAML) for admission; central PDP vs embedded policy; fail-open vs fail-closed; policy testing in CI (`opa test`/conftest).
- **Tests/evals:** policy unit tests; a denied tool call is actually blocked; admission rejects a non-compliant manifest.
- **Blog:** *"Authorizing agent tool-use with OPA external authz."*

### Track F — Zero-Trust Workload Identity & mTLS
**Goal:** encrypted, mutually-authenticated workload-to-workload traffic; portable identity.

- **Current:** Cilium identity + L7 proxy, but no encryption/mutual-auth.
- **Target:** **run both and compare** — (1) Cilium WireGuard + mutual auth (low-op), (2) SPIFFE/SPIRE issuing SVIDs for agent A2A / gateway mTLS (portable, spans VMs). The comparison *is* the deliverable.
- **Components:** Cilium encryption + mutual-auth config; SPIRE server/agent; SPIFFE IDs for agents/gateway.
- **Tradeoffs to articulate:** Cilium mesh-auth vs SPIRE (op cost vs portability/multi-cluster/VM reach); SVID rotation; identity bootstrapping/attestation; where mTLS terminates.
- **Tests/evals:** packet capture shows encryption; a workload without a valid SVID/identity is refused; SVID rotation continuity.
- **Blog:** *"SPIRE vs Cilium mesh-auth: two roads to workload mTLS (and when to pick which)."*

### Track G — Secrets, PKI & Supply Chain
**Goal:** mature the already-strong secrets/PKI base; close supply-chain gaps.

- **Current:** OpenBao + ESO + cert-manager; identity model documented; Trivy + Falco.
- **Target:** automate 1Password→OpenBao seeding (PushSecret/sync); OpenBao PKI as internal CA (feeds Track F); image signing (cosign) + SBOM/admission gate; rotate the OpenBao root token + scoped policies.
- **Tradeoffs to articulate:** static vs dynamic secrets; OpenBao PKI vs cert-manager issuers; signing/attestation (cosign/sigstore) enforcement cost.
- **Tests/evals:** unsigned image is rejected; dynamic secret mints & expires; Trivy critical gate.
- **Blog:** *"Dynamic secrets and an internal CA with OpenBao."*

---

## Testing & Evaluation strategy

A first-class, cross-cutting track (Track B) because "test and evaluate" is a core FDE deliverable. Six evaluation surfaces:

| Surface | Question it answers | Tooling to explore |
|---|---|---|
| **Model eval** | Which model is better for this task (quality/cost/latency)? | Langfuse, Phoenix/Arize, promptfoo, Braintrust |
| **Agent eval** | Did the agent pick the right tools and reach the goal (trajectory/task-success)? | DeepEval, Langfuse traces, custom scorers |
| **RAG/context eval** | Is retrieval grounded and faithful? | Ragas, DeepEval |
| **Performance/load** | Latency, throughput, tokens/sec, cost under load? | k6, Locust, gateway metrics |
| **Security/red-team** | Prompt injection, jailbreak, data exfil, tool abuse? | garak, PyRIT, promptfoo red-team |
| **Infra/policy** | Do policies, failover, and mTLS actually hold? | `opa test`/conftest, chaos (litmus), packet capture |

**Pipeline shape:** offline eval suites run in **CI on every prompt/agent change** (regression gate); **online eval** samples production traces (via Langfuse/Phoenix) for drift; **red-team** runs on a schedule. Model A/B is driven by the AI gateway's canary routing (Track C) with scores compared in the eval platform.

**Tradeoffs to articulate:** deterministic asserts vs LLM-as-judge vs human; offline vs online eval; eval cost/token budget; golden-dataset curation and drift.

---

## Blog series map

Working titles, grouped. Each maps to a track/milestone and doubles as portfolio evidence.

| # | Post | Track | Milestone |
|---|---|---|---|
| 1 | Wiring OTel through an agentic platform | A | M1 |
| 2 | Building the kagent/agentgateway observability dashboard | A | M1 |
| 3 | Observability vs Evaluation: why you need both | B | M1 |
| 4 | Evaluating agent tool-use: trajectory & task-success | B | M2 |
| 5 | LLM eval in CI: regression-gating prompt changes | B | M2 |
| 6 | Red-teaming agents: prompt injection & tool abuse | B | M4 |
| 7 | Putting an AI gateway in front of your agents | C | M2 |
| 8 | Model A/B testing via canary routing + eval scores | B+C | M3 |
| 9 | From `AUTH_MODE=unsecure` to OIDC agent identity | D | M2 |
| 10 | Authorizing agent tool-use with OPA external authz | E | M3 |
| 11 | SPIRE vs Cilium mesh-auth: two roads to workload mTLS | F | M4 |
| 12 | Dynamic secrets & an internal CA with OpenBao | G | M3 |
| 13 | The whole platform: a secured, evaluated agent stack (capstone) | all | M5 |

---

## Sequencing & milestones

Ordered by **value + dependency**, not by track. Each milestone is demoable and blog-able.

- **M0 — Foundation (in progress):** telemetry guardrails shipped (ExternalSecret + node/pod alerts); OOM alert fixed. ✅
- **M1 — See everything:** kagent OTel export → Tempo; agentgateway metrics scraped; MCP/agent dashboards; stand up an eval/LLM-obs platform (Langfuse or Phoenix) ingesting traces. *(Tracks A, B start)*
- **M2 — Control & identity:** AI gateway in front of LLM (routing + guardrails); Keycloak OIDC → kill `AUTH_MODE=unsecure` + SSO; first CI eval suite. *(C, D, B)*
- **M3 — Authorize & compare:** OPA ext-authz on tool/model calls; model A/B via canary routing scored in the eval platform; OpenBao PKI/dynamic secrets. *(E, C, G)*
- **M4 — Zero-trust & red-team:** Cilium mTLS **and** SPIRE (compare); security/red-team eval suite. *(F, B)*
- **M5 — Capstone:** end-to-end demo + write-up of the full secured, evaluated agent platform. *(all)*

---

## Success criteria — "what I can demonstrate"

By M5, the portfolio shows I can:
- Instrument an agentic system end-to-end (traces/metrics/logs/evals) and **read** it.
- Stand up an AI gateway with routing, guardrails, and cost control.
- Give agents/humans real identities (OIDC) and **authorize** their actions (OPA).
- Secure the data plane with mTLS via **two** approaches and explain the tradeoff.
- Run model + agent + security **evaluations**, gated in CI.
- And for each: **explain to a customer why** — the FDE differentiator.

---

## Open decisions

- Eval/LLM-obs platform: **Langfuse vs Phoenix** (both self-hostable, OTel-native). Lean Langfuse for the eval+prompt-mgmt breadth; validate in M1.
- AI gateway: **kgateway (full Gateway-API) vs agentgateway standalone** — decide in M2 based on routing/guardrail needs.
- Admission policy: **OPA/Gatekeeper vs Kyverno** — decide in M3.
- mTLS: run **both** Cilium mesh-auth and SPIRE (the comparison is intentional, not a decision to shortcut).

---

*Related: [`agent-identity-security-model.md`](./agent-identity-security-model.md) · tracking epic: (link once opened)*
