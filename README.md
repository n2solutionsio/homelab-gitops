# homelab-gitops

ArgoCD application manifests for the homelab k3s cluster.

## Structure

```
apps/           # ArgoCD Application manifests (watched by root app-of-apps)
manifests/      # Helm values and Kubernetes manifests per application
```

## How it works

A root ArgoCD Application (managed by Terraform in [homelab-live](https://github.com/n2solutionsio/homelab-live)) watches the `apps/` directory. Each YAML file there defines a child Application pointing to an upstream Helm chart with values from `manifests/`.

## Applications

| App | Chart | Namespace |
|-----|-------|-----------|
| cert-manager | jetstack/cert-manager | cert-manager |
| metallb | metallb/metallb | metallb-system |
| nfs-provisioner | nfs-subdir-external-provisioner | nfs-provisioner |
