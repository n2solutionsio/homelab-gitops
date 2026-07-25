#!/usr/bin/env bash
# Park / unpark Langfuse on demand to reclaim ~600m CPU + ~2.3Gi RAM when you're
# not evaluating. Data is preserved (databases sit on PVCs; this only scales the
# pods). Requires the ArgoCD replica-ignore in apps/langfuse.yaml, else self-heal
# scales it back up.
#
#   ./langfuse-scale.sh down   # park (UI + ingestion go offline; data kept)
#   ./langfuse-scale.sh up      # spin up before an eval session (~1-2 min)
set -euo pipefail
NS=langfuse
DEPLOYS=(langfuse-web langfuse-worker langfuse-s3)      # compute + object store
STS=(langfuse-clickhouse-shard0 langfuse-zookeeper)     # data layer
# NOTE: PostgreSQL is external (shared cluster) — never touched here.

case "${1:-}" in
  down)
    kubectl -n "$NS" scale deploy "${DEPLOYS[@]}" --replicas=0          # compute first
    kubectl -n "$NS" scale statefulset "${STS[@]}" --replicas=0         # then data
    echo "Langfuse parked (scaled to 0). Data preserved on PVCs."
    ;;
  up)
    kubectl -n "$NS" scale statefulset "${STS[@]}" --replicas=1         # data first
    kubectl -n "$NS" scale deploy "${DEPLOYS[@]}" --replicas=1          # then compute
    echo "Waiting for langfuse-web..."
    kubectl -n "$NS" rollout status deploy/langfuse-web --timeout=240s
    echo "Langfuse up → https://langfuse.homelab.n2solutions.io"
    ;;
  *)
    echo "usage: $0 up|down" >&2; exit 1 ;;
esac
