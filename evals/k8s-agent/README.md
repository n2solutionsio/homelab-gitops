# Eval 01 — k8s-agent quality (runner)

First eval on the Langfuse platform. Invokes k8s-agent against
[`dataset-v1.yaml`](./dataset-v1.yaml), scores **correctness** (vs kubectl-computed
ground truth) and **tool-use**, and pushes a Langfuse dataset run. Design:
[`docs/eval-01-k8s-agent-quality.md`](../../docs/eval-01-k8s-agent-quality.md).

## Prereqs (GitOps-managed, sync with the kagent app)
- `manifests/kagent/resources/eval-runner.yaml` — `eval-runner` SA, read-only
  ClusterRole, `langfuse-eval-keys` ExternalSecret (langfuse + anthropic keys).
- `manifests/kagent/resources/eval-runner-configmap.yaml` — the runner + dataset,
  **generated** from `runner.py` + `dataset-v1.yaml` (regen command in its header
  after you change either).

## Run an ad-hoc eval

```bash
kubectl -n kagent create -f evals/k8s-agent/job.yaml
kubectl -n kagent logs -f job/<job-name>   # name printed above
```

If Langfuse is **up**, results land at `langfuse.homelab.n2solutions.io` (dataset
`k8s-agent-v1`, run `baseline-<ts>`) with `correct`/`tool_used`/`judge` scores.
If Langfuse is **parked** (see `../langfuse-scale.sh`), scores are still computed
and printed — storage is just skipped.

## Regression gate (M-eval-4)

`manifests/kagent/resources/eval-regression-gate-cronjob.yaml` runs the eval
**weekly** with `EVAL_GATE=1`, exiting non-zero if accuracy / tool-use / judge
drops below its floor (`EVAL_MIN_*`). A failed gate Job trips the
**`AgentEvalRegression`** alert (Alertmanager → Slack).

```bash
# trigger the gate on demand (e.g. after changing an agent prompt/model)
kubectl -n kagent create job --from=cronjob/eval-regression-gate eval-gate-manual
kubectl -n kagent logs -f job/eval-gate-manual   # look for GATE: PASS / FAIL
```

## Scope
- ✅ Deterministic correctness + tool-use scoring
- ✅ LLM-as-judge on reasoning items (M-eval-3)
- ✅ Scheduled regression gate + alert (M-eval-4)
