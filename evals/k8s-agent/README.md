# Eval 01 — k8s-agent quality (runner)

First eval on the Langfuse platform. Invokes k8s-agent against
[`dataset-v1.yaml`](./dataset-v1.yaml), scores **correctness** (vs kubectl-computed
ground truth) and **tool-use**, and pushes a Langfuse dataset run. Design:
[`docs/eval-01-k8s-agent-quality.md`](../../docs/eval-01-k8s-agent-quality.md).

## Prereqs (GitOps-managed)
`manifests/kagent/resources/eval-runner.yaml` provides the `eval-runner`
ServiceAccount, read-only ClusterRole, and the `langfuse-eval-keys` ExternalSecret.
It syncs with the kagent app.

## Run

```bash
# 1. Package the runner + dataset into a ConfigMap (from the repo files)
kubectl -n kagent create configmap eval-runner \
  --from-file=runner.py=evals/k8s-agent/runner.py \
  --from-file=dataset-v1.yaml=evals/k8s-agent/dataset-v1.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

# 2. Launch the run
kubectl -n kagent create -f evals/k8s-agent/job.yaml

# 3. Watch
kubectl -n kagent logs -f job/<job-name>   # name printed by step 2
```

Results land in Langfuse (`langfuse.homelab.n2solutions.io`) as dataset
**`k8s-agent-v1`**, run **`baseline-<timestamp>`**, with `correct` and
`tool_used` scores per item. The job log prints a summary (accuracy, tool-use rate).

## Scope
- ✅ Deterministic correctness + tool-use scoring (this runner).
- ⏳ Reasoning items are logged but unscored — LLM-as-judge is **M-eval-3**.
- ⏳ Scheduling as a regression gate is **M-eval-4**.
