# OpenBao — terraform CI auth (bootstrap)

Kubernetes-auth wiring that lets the ARC-runner-based **gated terragrunt CI**
fetch its secrets from OpenBao at job time — no static CI secrets. Extends the
same k8s auth method External Secrets Operator already uses.

> **Applied out-of-band via the `bao` CLI.** OpenBao is the auth control-plane,
> so a chicken-and-egg makes full config-as-code awkward. These files are the
> source-of-truth intent plus a reproducible runbook. Future improvement:
> manage policies/roles with the Terraform `vault` provider.

## Components

- **Auth role** `terraform-ci` (mount `kubernetes/`) — binds SA
  `gha-runner-set-gha-rs-no-permission` in namespace `arc-runners` to the policy
  below, with a 20-minute token TTL.
- **Policy** `terraform-ci` (`terraform-ci.hcl`) — read-only on three KV v2
  paths, nothing else.
- **KV secrets** (mount `secret`, v2), populated from 1Password:

  | path | fields |
  |---|---|
  | `tfstate-r2` | `access_key_id`, `secret_access_key`, `account_id` |
  | `k3s-cluster-token` | `token` |
  | `proxmox-automation-token-secret` | `Secret`, `host`, `token_id`, `endpoint` |
  | `unifi-terraform` | `username`, `password` |

## ⚠️ Security note

The role binds the **shared** runner SA, so any job on those runners could read
these secrets. Hardening step: a dedicated runner scale set + ServiceAccount
used only by the terraform workflow.

## Reproduce / apply

```bash
BAO() { kubectl exec -i -n openbao openbao-0 -- env \
  BAO_TOKEN="$(op read 'op://homelab/openbao-root-token/token')" \
  BAO_ADDR=http://127.0.0.1:8200 bao "$@"; }

# policy
BAO policy write terraform-ci - < terraform-ci.hcl

# role (modeled on the existing external-secrets role)
BAO write auth/kubernetes/role/terraform-ci \
  bound_service_account_names=gha-runner-set-gha-rs-no-permission \
  bound_service_account_namespaces=arc-runners \
  token_policies=terraform-ci token_ttl=20m ttl=20m

# secrets (values sourced from 1Password; never echoed)
BAO kv put secret/tfstate-r2 \
  access_key_id="$(op read 'op://homelab/Cloudflare R2 Api Token/Access_Key_ID')" \
  secret_access_key="$(op read 'op://homelab/Cloudflare R2 Api Token/Secret_Access_Key')" \
  account_id="<r2-account-id>"
BAO kv put secret/k3s-cluster-token \
  token="$(op read 'op://homelab/k3s-cluster-token/credential')"
BAO kv patch secret/proxmox-automation-token-secret \
  token_id="$(op read 'op://homelab/proxmox-automation-token-secret/Token ID')" \
  endpoint="https://<proxmox-host>:8006/"
```

## Verify (least-privilege)

```bash
CI=$(BAO token create -policy=terraform-ci -ttl=5m -field=token)
# with the CI token: the 3 paths above read OK; any other secret returns 403.
```

## How the CI consumes it (next: workflow wiring)

The runner authenticates with its projected ServiceAccount JWT and reads the
secrets at job start:

```bash
JWT=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
export BAO_ADDR=http://openbao.openbao.svc.cluster.local:8200
BAO_TOKEN=$(bao write -field=token auth/kubernetes/login role=terraform-ci jwt="$JWT")

export AWS_ACCESS_KEY_ID=$(bao kv get -field=access_key_id secret/tfstate-r2)
export AWS_SECRET_ACCESS_KEY=$(bao kv get -field=secret_access_key secret/tfstate-r2)
export R2_ACCOUNT_ID=$(bao kv get -field=account_id secret/tfstate-r2)
export PROXMOX_VE_ENDPOINT=$(bao kv get -field=endpoint secret/proxmox-automation-token-secret)
export PROXMOX_VE_API_TOKEN="$(bao kv get -field=token_id secret/proxmox-automation-token-secret)=$(bao kv get -field=Secret secret/proxmox-automation-token-secret)"
export K3S_TOKEN=$(bao kv get -field=token secret/k3s-cluster-token)
export UNIFI_USERNAME=$(bao kv get -field=username secret/unifi-terraform)
export UNIFI_PASSWORD=$(bao kv get -field=password secret/unifi-terraform)
```

Note: snippet uploads still require SSH-as-root to the Proxmox host, but the k3s
cloud-init snippets are frozen (module ≥ v0.7.3), so routine plan/apply never
touches them — the API token above is sufficient.
