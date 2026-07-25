# Eval 01 — k8s-agent quality (scope)

**Status**: Scoped, not yet built
**Track**: B (Testing & Evaluation) · builds on the Langfuse platform stood up in M1
**Owner**: Nick Narcise

The first real evaluation on the platform. Goal: turn "the agent seems to work" into a
**reproducible, scored baseline** we can regress against and A/B — the core FDE skill of
*proving* an agent's quality, not just running it.

---

## Why k8s-agent first

- **Well-exercised** — it's the agent used throughout the observability build; traces already flow to Langfuse.
- **Verifiable** — it answers factual cluster questions via kubectl MCP tools, so ground truth is *computable* (unlike open-ended chat).
- **Tool-using** — lets us evaluate not just the answer but the *trajectory* (did it call the right tool?).

Once the harness works for k8s-agent, it generalizes to the other agents (helm, promql, argocd, …).

## What we evaluate (three dimensions)

| Dimension | Question | Method |
|---|---|---|
| **Correctness** | Is the answer right? | Deterministic match (numeric/factual) + LLM-as-judge (reasoning) |
| **Tool-use / trajectory** | Did it call the right tool with sane args? | Assertion over the trace's `execute_tool` spans |
| **Cost & latency** | Tokens and time per query | Read from the trace's `gen_ai.usage.*` + duration (already captured) |

## Dataset design

A curated set (~15 items to start) of k8s questions, each with a **ground-truth strategy**.
Because the cluster is live, "expected" is either a **stable fact** or **computed at eval time**.

```yaml
# dataset item shape
- id: nodes-count
  input: "How many nodes are in the cluster? Answer with just the number."
  agent: k8s-agent
  ground_truth:
    type: computed            # runner derives expected via kubectl at run time
    check: "kubectl get nodes --no-headers | wc -l"
    match: exact              # exact | numeric | contains | judge
  expect_tool: k8s_get_resources
- id: kube-system-running
  input: "How many running pods in kube-system?"
  ground_truth:
    type: computed
    check: "kubectl -n kube-system get pods --field-selector=status.phase=Running --no-headers | wc -l"
    match: numeric
  expect_tool: k8s_get_resources
- id: why-crashloop
  input: "A pod is in CrashLoopBackOff. Walk through how you'd diagnose it."
  ground_truth:
    type: judge               # no single answer — score reasoning quality
    rubric: "Mentions: check logs (kubectl logs --previous), describe/events, resource limits/OOM, image/config. Penalize destructive actions without confirmation."
    match: judge
```

Item categories: **deterministic factual** (counts, states, limits — cheap exact/numeric checks) and **reasoning** (diagnosis/explanation — LLM-as-judge against a rubric). Start ~10 deterministic + ~5 reasoning.

## Eval methods

1. **Deterministic** — runner computes ground truth (runs the `check` command), compares to the agent's answer (`exact`/`numeric`/`contains`). Cheap, reliable, no judge tokens. Best for factual items.
2. **LLM-as-judge** — for reasoning items, an Anthropic-backed judge scores the answer 0–1 against the rubric. Use **Langfuse's built-in LLM-as-judge evaluator** (configure an Anthropic connection in Langfuse) so scoring is a platform feature, not bespoke code.
3. **Tool-use assertion** — from the trace, assert an `execute_tool <expect_tool>` span exists. Catches "right answer, wrong/no tool" (e.g. hallucinated from memory).

## Execution architecture (Langfuse dataset run)

The canonical Langfuse flow — reproducible and comparable across runs:

```
Langfuse Dataset "k8s-agent-v1" (the items above)
        │
   ┌────▼─────────── eval runner (k8s Job / local script, Langfuse SDK) ───────────┐
   │ for each item:                                                                 │
   │   1. invoke k8s-agent via A2A (kagent-controller /api/a2a/kagent/k8s-agent/)   │
   │   2. the run already emits an OTel trace → Langfuse (existing pipeline)         │
   │   3. link that trace to a dataset-run item (Langfuse SDK)                       │
   │   4. deterministic score (compute ground truth, compare) + tool-use assertion  │
   └────┬───────────────────────────────────────────────────────────────────────────┘
        │
   Langfuse applies the LLM-as-judge evaluator to reasoning items
        │
   Dataset Run "k8s-agent-v1 @ <date>/<model>"  →  aggregate scores
```

Each **dataset run** is a labeled, scored snapshot. Re-run after a prompt change / model swap → compare runs side by side. This is also how model A/B works later (run the same dataset through the AI-gateway routed to model A vs B — Track C).

## Metrics & baseline

Per run, surfaced in Langfuse:
- **Accuracy** — % of deterministic items correct
- **Judge score** — mean LLM-as-judge (0–1) on reasoning items
- **Tool-use rate** — % that called the expected tool
- **Cost** — mean input/output tokens per query (from `gen_ai.usage.*`)
- **Latency** — mean trace duration

First run = the **baseline**. Success criterion for v1 is not a number — it's *having a repeatable scored run* we can regress against. (A reasonable target once tuned: ≥90% deterministic accuracy, ≥0.8 judge, 100% tool-use.)

## Prerequisites & decisions

- [ ] **Langfuse LLM connection** — add an Anthropic connection in Langfuse (reuse the `ai-org/anthropic` key) so the built-in LLM-as-judge can run. *(UI + secret.)*
- [ ] **Runner placement** — k8s `Job` in-cluster (can reach the A2A endpoint directly) vs local script. Lean **in-cluster Job** — closest to how it'd run scheduled/CI later.
- [ ] **Ground-truth for live data** — use `computed` checks (runner derives expected at run time) so items don't rot as the cluster changes. Avoid hard-coded counts.
- [ ] **Dataset authoring** — curate the ~15 items (this doc has the shape; needs the full set).
- [ ] **Judge model/rubric** — Sonnet as judge; per-item rubric. Decide a shared correctness rubric vs per-item.

## Build plan (phased)

1. **M-eval-1** — author the dataset (~15 items) + create it in Langfuse. Configure the Anthropic LLM connection.
2. **M-eval-2** — the runner (invoke agent → link trace → deterministic + tool-use scores). Produce the first **baseline run**.
3. **M-eval-3** — add LLM-as-judge on reasoning items; full scored run.
4. **M-eval-4** — schedule it (CI / cron) as a **regression gate** on agent-prompt changes; alert on score drop.

## Blog angle

*"Evaluating a Kubernetes AI agent: datasets, LLM-as-judge, and tool-use scoring."* Strong FDE portfolio piece — shows the full loop from telemetry → dataset → scored, reproducible eval, with the honest nuance of ground-truth on a live cluster.

---

*Related: [`ai-platform-roadmap.md`](./ai-platform-roadmap.md) (Track B). Platform stood up in homelab-gitops #190–#196.*
