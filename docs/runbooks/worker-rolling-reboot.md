# Runbook: Rolling reboot of k3s worker nodes

**Purpose**: Recover from accumulated containerd / kubelet runtime instability on worker nodes. Symptoms include:
- Pods stuck `Terminating` or `Init:0/N` for many minutes
- `exec` / `setns` errors in container logs (`failed to exec in container ... setns process: exit status 1`)
- `CreateContainerError` with `name "..." is reserved for "<other-hash>"` (orphaned containerd state)
- Probes timing out → kubelet kill-restart loops
- Multiple unrelated pods on the same node going unhealthy

**When to use**: After confirming the issue is node-level (multiple unrelated pods affected, same nodes, no app-config explanation). Captured during 2026-06-19 incident.

**Total time**: ~5-10 min per worker, sequential. Plan ~30 min for all three.

**Blast radius**: Each worker reboot evicts its pods. Workloads with single replicas (Prometheus, Grafana, Loki, postgres, valkey, orchestrator) will be briefly unavailable during failover. NFS-backed storage means PVCs survive cleanly.

---

## Pre-flight

1. **Confirm etcd quorum is OK** (k3s embedded etcd):
   ```bash
   kubectl get nodes -l node-role.kubernetes.io/control-plane=true
   # Expect: at least 1 Ready control-plane node (we run 1)
   ```
   ⚠️ If you have multiple control-plane nodes and any are NotReady, fix that first.

2. **Confirm NFS provisioner is healthy** (pod migrations need it):
   ```bash
   kubectl -n nfs-provisioner get pod
   # Expect: 1/1 Running
   ```

3. **Identify which workers actually need rebooting** (don't reboot what's healthy):
   ```bash
   # Pods with the runtime-error signature:
   kubectl get pods -A --no-headers | awk '$3!~/[1-9]\/[1-9]$/ && $4!="Completed"'

   # Cross-reference their nodes:
   kubectl get pod <NS>/<POD> -o jsonpath='{.spec.nodeName}{"\n"}'
   ```
   A worker with ≥2 unhealthy pods that aren't explainable by app config is a reboot candidate.

4. **Note which workers host stateful single-replica workloads** so you know what'll move:
   ```bash
   for W in homelab-k3s-worker-{0,1,2}; do
     echo "=== $W ==="
     kubectl get pods -A --field-selector spec.nodeName=$W --no-headers \
       | awk '{print "  " $1 "/" $2}' | head -20
   done
   ```

---

## Per-worker procedure

Repeat for each worker that needs reboot, **one at a time**, verifying between each.

### 1. Cordon
```bash
WORKER=homelab-k3s-worker-1   # set per iteration
kubectl cordon "$WORKER"
```

### 2. Drain
```bash
kubectl drain "$WORKER" \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --grace-period=30 \
  --timeout=120s
```

**Expected**: pods evict cleanly, command completes. If you see `error when waiting for pod ... to terminate: context deadline exceeded` for specific pods, those are stuck (runtime can't kill them). Force-delete them:
```bash
kubectl -n <NS> delete pod <POD> --force --grace-period=0
```
Daemonsets stay on the node (correct — they reboot with it).

### 3. SSH to the node and reboot

> **Node SSH access**: login user is **`ubuntu`** (cloud-init `ciuser`), key `~/.ssh/homelab_automation`
> (stored in 1Password `homelab` vault as `homelab-automation-ssh`). Passwordless sudo is enabled.
> Verify the user with `qm config <vmid>` on the Proxmox host if in doubt — don't assume.

```bash
# from your laptop:
ssh -i ~/.ssh/homelab_automation ubuntu@<WORKER-IP>   # IPs: worker-0=10.30.30.11, worker-1=10.30.30.12, worker-2=10.30.30.13
sudo reboot
# connection drops — that's expected
```

Alternative if SSH is broken: reboot the VM from the Proxmox web UI (use **Reboot** = graceful ACPI, not Stop/Start).

### 4. Wait for it to come back
```bash
until kubectl get node "$WORKER" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}' | grep -q True; do
  date '+%H:%M:%S waiting for node to be Ready...'
  sleep 5
done
echo "Node Ready"
```
Typical wall-clock: 90-120s for VM boot + kubelet register.

### 5. Verify daemonset pods come back
```bash
kubectl get pods -A --field-selector spec.nodeName="$WORKER" --no-headers \
  | awk '$3!~/[1-9]\/[1-9]$/' | head
# Expect: nothing (all daemonset pods Ready)
```
If a daemonset pod is still unhealthy here, the runtime issue MAY have survived the reboot — check that node's containerd version + disk usage before proceeding.

### 6. Uncordon
```bash
kubectl uncordon "$WORKER"
```

### 7. Wait ~2 min, then verify previously-sick pods recovered
```bash
sleep 120
kubectl get pods -A --no-headers | awk '$3!~/[1-9]\/[1-9]$/ && $4!="Completed"'
```
Pods that were unhealthy on this worker should now be Running cleanly somewhere. If any are still in error states, debug those individually.

---

## Post-flight (after all workers done)

1. **All nodes Ready, none SchedulingDisabled**:
   ```bash
   kubectl get nodes
   ```

2. **No pods in non-Running/non-Completed state**:
   ```bash
   kubectl get pods -A --no-headers | awk '$3!~/[1-9]\/[1-9]$/ && $4!="Completed"'
   # Expect: empty
   ```

3. **Monitoring stack healthy**:
   ```bash
   kubectl -n monitoring get pod
   # Expect: prometheus 2/2, grafana 3/3, alertmanager 2/2
   ```

4. **ArgoCD apps all Synced + Healthy**:
   ```bash
   kubectl -n argocd get app -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.sync.status}/{.status.health.status}{"\n"}{end}'
   # Expect: every line ends with Synced/Healthy
   ```

5. **Spot-check a critical workload**: send a test Slack message to the orchestrator and verify it responds.

---

## If reboots DON'T fix it

Indicates a deeper problem than accumulated runtime state. Investigate:

- **Containerd version**: `ssh ubuntu@<WORKER>; sudo crictl version` — k3s ships containerd; very old versions have known bugs. Recent k3s upgrade may be needed.
- **Disk pressure**: `ssh ubuntu@<WORKER>; df -h /` — if >80%, prune images: `sudo k3s crictl rmi --prune` and vacuum logs: `sudo journalctl --vacuum-time=3d`. (2026-06-20: worker-0 hit 84% from 95d of image cache + journal; prune+vacuum took it to 44%.)
- **Kernel logs**: `ssh ubuntu@<WORKER>; sudo dmesg -T | tail -100` — look for OOM kills, cgroup errors, NFS timeouts.
- **k3s service**: `sudo journalctl -u k3s-agent --since "1 hour ago" | tail -50` — look for repeated errors.

---

## Lessons captured from the 2026-06-19 incident

- `kubectl drain` with default timeout often hangs on pods the runtime can't kill — pass `--timeout=120s` and follow up with `--force --grace-period=0` for the holdouts
- ArgoCD sync can hang **indefinitely** waiting for an unhealthy workload to recover, blocking unrelated config updates from being applied — symptom: `operationState.phase: Running` for >10 min, `message: waiting for healthy state of <something>`. Fix: restart the argocd-application-controller statefulset, OR (better) eliminate the unhealthy workload so the sync naturally completes
- Resource limit changes won't help if the runtime can't start the new pod in the first place — pivot to node maintenance, not config tuning
- StatefulSet pods on NFS-backed PVCs are safe to reschedule across nodes (no node pinning despite RWO access mode), so node reboots don't risk data loss for monitoring / postgres / loki / valkey
