# Design: Agent Identity & Secret-Access Security Model

**Status**: In progress (Step 1 landed 2026-06-28 via PR #164)
**Owner**: Nick Narcise
**Scope**: How humans, AI agents, and in-cluster workloads authenticate to the
homelab cluster, GitHub, and secret stores — and how we keep long-lived
credentials out of places an agent or attacker can read them.

---

## Problem

We kept having to roll credentials (GitHub PATs especially) because they were
readable by the Claude Code agent and leaked into transcripts/backups. Rolling
cleans up *after* exposure but never removes the *cause*.

**Root cause:** the Claude Code agent runs as the human user, on the human's
unlocked laptop, and therefore **inherits every ambient credential the user
has**. Where a secret is *stored* does not constrain the agent — only the
agent's *execution context* does.

### Demonstrated exposure (2026-06-28)

In a single session the agent reached secrets through three independent systems:

| System | Gate when the agent accessed it | Severity |
|---|---|---|
| **kubeconfig** (`system:masters`) | none — read every cluster Secret silently | 🔴 total + invisible |
| **gh / git** | none — keyring/env token used silently | 🟠 |
| **1Password** | biometric prompt per read/session | 🟢 already gated |

Key facts:
- The laptop kubeconfig authenticated as `system:admin` in group
  `system:masters`, which **bypasses RBAC entirely** — it cannot be restricted,
  only replaced.
- `op` is authorized via the **1Password desktop-app CLI integration**
  (`~/.config/op/op-daemon.sock`) with Touch ID. Biometrics gate the *unlock*,
  not necessarily each read (session cache). This is the strongest of the three.
- `gh` prefers the `GITHUB_TOKEN` env var over its keyring. `n2secrets()` in
  `~/.zshrc` exports a (now dead) `GITHUB_TOKEN` into the shell, which the agent
  inherits — both a leak vector and the reason `gh` kept failing after re-auth.

---

## Target identity model (three tiers)

Each principal gets the **least** access it needs, and a **distinct identity**
so actions are attributable.

| Identity | Access | Credential source | Lifetime |
|---|---|---|---|
| **Agent** (Claude Code) | read-only, **no Secrets** | k8s ServiceAccount token | short |
| **Human — normal ops** | read-only (writes go through GitOps) | **OpenBao**-minted, on demand | minutes–1h |
| **Break-glass** | full `system:masters` admin | k3s admin cert, sealed in 1Password | emergencies only |

Day-to-day, the human does **not** mutate the cluster directly — **GitOps
(ArgoCD) makes changes**. Direct access is for read/debugging (dynamic
read-only token) or emergencies (break-glass).

---

## Vault & secret-store layout

| Store | Role | Cluster-readable? |
|---|---|---|
| **OpenBao** | source of truth for **workload** secrets (what pods read) | yes (ESO, via k8s auth) |
| **1Password `homelab`** | machine-readable static secrets | yes (ESO `onepassword` store, pinned to this vault) |
| **1Password `homelab-ops`** | human/operator + **break-glass** secrets | **no** — no store points here |
| **1Password `n2solutions`** | human operator org secrets (Anthropic, etc.) | no |

Rule: **only put in `homelab` what the cluster legitimately consumes.**
Operator and break-glass material lives in `homelab-ops` / `n2solutions`, which
ESO has no path to.

Current GitHub PAT (the orchestrator's): stored at
`op://homelab-ops/n2-orchestrator Github Classic PAT/token` (human copy) and
seeded into OpenBao at `secret/ai-org/github` (`pat-token`) where the
orchestrator reads it. **1Password → OpenBao seeding is currently manual**
(no PushSecret / sync job exists).

---

## Roadmap

### Step 0 — Sandbox the agent (foundation)
The only thing that isolates the agent from the human's privileges is
**execution-context isolation**: run Claude Code somewhere that does *not* have
the user's op daemon socket, admin kubeconfig, or gh keyring.
- Option A: a **separate macOS user** for agent work (own scoped kubeconfig, no
  1Password integration).
- Option B: a **container/devcontainer/VM** with only the narrow creds it needs.

Without this, Steps 1–6 are tidy but not *enforceable* — the agent can still
read the human's ambient credentials.

### Step 1 — Agent read-only identity (no Secrets) ✅ landed
- `claude-agent` namespace + ServiceAccount (`manifests/claude-agent/`).
- Bound to built-in `view` ClusterRole (excludes core v1 Secrets) + read-only
  on `kagent.dev` / `argoproj.io` / `external-secrets.io` CRDs.
- Local: mint a short-lived token (`kubectl create token claude-agent -n
  claude-agent --duration=8h`), build `~/.kube/claude-agent.config`, run Claude
  Code with `KUBECONFIG` pointed at it.
- Verify: `kubectl auth can-i get secrets -A` → **no**.
- Side benefit: agent actions become attributable
  (`system:serviceaccount:claude-agent:claude-agent`).

### Step 2 — Remove plaintext secrets from the laptop
- Drop the `GITHUB_TOKEN` export from `n2secrets()` (broad shell export →
  inherited by every child process including the agent).
- Replace with per-process injection: `op run --env-file=… -- <command>`.
- Delete `GITHUB_TOKEN` from `orchestrator/.env`; inject via `op run` for local
  dev. Remove the dead Copilot MCP token from `~/.claude.json`.

### Step 3 — Dynamic human cluster access + kill static PATs
- Enable an OpenBao **Kubernetes secrets engine** (distinct from the existing
  k8s *auth* method) that mints short-lived, bound SA tokens on request.
- Add a human auth method to OpenBao (OIDC preferred, else userpass).
- Human role → read-only operator; **remove the admin cert from the laptop**.
- Migrate the orchestrator's GitHub PAT → **GitHub App** + ESO
  `githubaccesstokens` generator (1h installation tokens, fine-grained,
  non-personal). Decouples prod from the human's PAT; ends manual rolling.

### Step 4 — Break-glass
- Seal the k3s `system:masters` admin cert in `op://homelab-ops/…`.
- Documented, access-logged, used only when OpenBao itself is down (it is
  single-replica: `openbao-0`) or a true emergency.

### Step 5 — GitOps-ify everything
- k8s RBAC → ArgoCD (already the path for Step 1).
- OpenBao config (engine mounts, roles, policies, auth) → **Terraform**
  (`vault` provider works against OpenBao).

### Step 6 — Broker / workload-identity endgame
- Run MCP servers in-cluster (kmcp `MCPServer`), creds mounted from OpenBao.
- Front them with **agentgateway**: terminates caller mTLS, enforces **OPA**
  per-tool/per-method authz, **injects** the upstream credential so the caller
  never sees it. Central audit.
- **SPIFFE/SPIRE** as the identity root: in-cluster workloads get SVIDs
  (no stored bearer tokens); OpenBao consumes SPIFFE JWT-SVIDs via its JWT auth
  method; agentgateway authenticates callers by SPIFFE ID. SPIRE registration
  entries are `ClusterSPIFFEID` CRDs → GitOps-managed.
  - Scope note: SPIFFE/SPIRE is **workload (machine) identity** — it does **not**
    apply to humans (OIDC) or the laptop agent (impractical). Its real payoff is
    a single identity plane across **Proxmox VMs + k8s**, at the gateway layer.
    It is deliberate operational weight; adopt at Step 6, not before. The 80/20
    until then is k8s SA projected tokens (already in use).

---

## Auditability

| Layer | Mechanism | Status |
|---|---|---|
| Access changes | GitOps PR review + ArgoCD sync history | ✅ in place |
| K8s runtime | `kube-apiserver` audit log + policy → Loki | ❌ **off by default in k3s** |
| OpenBao | audit device (file/syslog) → every mint/read | ❌ to enable (Steps 3–4) |
| 1Password | item-access activity log (break-glass retrieval) | ✅ built-in |
| Runtime security | Falco | ✅ deployed |
| Orchestrator app | append-only `audit.py` action log | ✅ in code |

Two real to-dos: **enable K8s API-server audit logging** (alongside Step 1, so
the new per-identity attribution is actually recorded) and **OpenBao audit
devices** (inside Steps 3–4).

---

## Open decision — secret source of truth (A vs B)

We currently have **two** wired stores: OpenBao (in use) and 1Password
`homelab` (idle). Pick one model deliberately:

- **A (current):** OpenBao = source of truth for workload secrets; 1Password =
  human/break-glass only. Simplest from here.
- **B:** 1Password `homelab` = source of truth for *static* secrets (ESO syncs
  from it); OpenBao reserved for *dynamic* creds (Step 3 k8s secrets engine).
  Cleaner long-term, but a deliberate migration.

Do **not** fork a single secret's source reactively (e.g. mid-incident).
