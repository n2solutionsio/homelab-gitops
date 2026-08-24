# Policy: terraform-ci
#
# Grants the ARC-runner-based gated terragrunt CI read-only access to exactly
# the secrets a `terragrunt plan`/`apply` needs — the R2 state-backend creds,
# the Proxmox automation API token, and the k3s cluster token. Nothing else.
#
# Bound to the ARC runner ServiceAccount via the `terraform-ci` Kubernetes auth
# role (see README.md). KV v2, so reads target the `secret/data/*` paths.

path "secret/data/tfstate-r2" {
  capabilities = ["read"]
}

path "secret/data/k3s-cluster-token" {
  capabilities = ["read"]
}

path "secret/data/proxmox-automation-token-secret" {
  capabilities = ["read"]
}

path "secret/data/unifi-terraform" {
  capabilities = ["read"]
}

# Public half of the automation SSH key, consumed as a cloud-init input by
# compute/demo-kubeadm. Only the public key is stored under this path; the
# private half stays in 1Password and AWX and is never readable by CI.
path "secret/data/homelab-automation-ssh" {
  capabilities = ["read"]
}
