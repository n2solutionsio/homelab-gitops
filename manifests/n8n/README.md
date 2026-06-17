# n8n — alert incident pipeline

This document describes the n8n workflow that turns every firing Alertmanager
alert into a tracked GitHub issue, and clears it when the alert resolves.

## Architecture

```
Alertmanager (severity=warning|critical)
    │  HTTP POST with full alert payload
    ▼
n8n webhook  (path: /webhook/alertmanager-incident)
    │
    ▼
n8n workflow "Alert Incident Pipeline"
    │  (parse → loop → dedup by fingerprint → create/update issue)
    ▼
GitHub n2solutionsio/homelab-gitops
    (issues labeled `incident` + `severity:<level>` + `namespace:<ns>`)
```

The wiring on the Alertmanager side is in `manifests/monitoring/values.yaml`
under `alertmanager.config.receivers.n8n-incidents`. The credential for n8n's
GitHub node is provisioned by `resources/external-secret-github.yaml`.

## One-time setup

### 1. OpenBao: store a fine-grained GitHub PAT

Generate a PAT at <https://github.com/settings/tokens?type=beta>:
- Repository access: `n2solutionsio/homelab-gitops`
- Permissions: **Issues** Read & write, **Contents** Read-only

Store it:
```bash
bao kv put kv/n8n-github token=ghp_xxxxx
```

ArgoCD syncs the ExternalSecret automatically; the resulting Secret
`n8n-github-credentials` in the `n8n` namespace contains `GITHUB_TOKEN`.

### 2. n8n: create the GitHub credential

In n8n UI (`https://n8n.homelab.n2solutions.io`):
1. **Credentials → New → GitHub API**
2. Authentication: `Access Token`
3. Token: paste from the Secret
   (`kubectl -n n8n get secret n8n-github-credentials -o jsonpath='{.data.GITHUB_TOKEN}' | base64 -d`)
4. Save as `GitHub - homelab-gitops`

### 3. n8n: import / build the workflow

You can either import the JSON exported to `workflows/alert-incident-pipeline.json`
(once we've exported a known-good version) or build it from scratch using the
node-by-node spec below. Either way, the webhook node's **path** must be
`alertmanager-incident` so the URL matches what Alertmanager is configured to
hit.

## Workflow design

### Nodes

| # | Node | Purpose |
| --- | --- | --- |
| 1 | **Webhook** | HTTP trigger. POST. Path: `alertmanager-incident`. Response: respond immediately with `200 OK` (Alertmanager retries on non-2xx). |
| 2 | **Code** (parse) | Take `$json.body.alerts[]` from the Alertmanager payload, normalize one item per alert with the fields the rest of the workflow needs (see "Normalized item shape" below). |
| 3 | **GitHub** (search existing) | Search Issues `repo:n2solutionsio/homelab-gitops state:open label:incident label:fingerprint:{{fingerprint}}`. Returns 0 or 1 hits. |
| 4 | **IF** | Branch: existing issue found? |
| 5a | (firing + no issue) **GitHub Create Issue** | Title: `[<severity>] <alertname> — <ns>/<pod>`. Body: full alert details (Markdown table). Labels: `incident`, `severity:<level>`, `namespace:<ns>`, `fingerprint:<value>`, `alertname:<value>`. |
| 5b | (firing + existing issue) **GitHub Comment** | "Alert re-fired at `<startsAt>`. Still firing." (deduplicates noisy repeat fires.) |
| 5c | (resolved + existing issue) **GitHub Comment** + **Close Issue** | "Resolved at `<endsAt>` (duration: `<delta>`). Auto-closing." |
| 5d | (resolved + no issue) — | No-op. (Resolve fired before n8n saw the start; happens during cluster bootstrap.) |

### Normalized item shape (output of node 2)

```js
return $input.first().json.body.alerts.map(a => ({
  json: {
    fingerprint: a.fingerprint,
    status: a.status,                                  // 'firing' | 'resolved'
    severity: a.labels.severity,                       // 'critical' | 'warning'
    alertname: a.labels.alertname,
    namespace: a.labels.namespace || 'cluster-wide',
    pod: a.labels.pod || null,
    container: a.labels.container || null,
    node: a.labels.node || a.labels.instance || null,
    startsAt: a.startsAt,
    endsAt: a.endsAt,
    description: a.annotations.description || '',
    summary: a.annotations.summary || '',
    runbook_url: a.annotations.runbook_url || '',
    generatorURL: a.generatorURL,                      // link back to the Prometheus rule
    raw: a,
  },
}));
```

### Dedup key: `fingerprint`

Alertmanager assigns a stable `fingerprint` to each unique label-set. It does
NOT change across firing → resolved → re-firing cycles for the same alert
condition. Using `fingerprint:<value>` as a GitHub label gives us cheap O(1)
lookups via the search API.

### Issue title / body conventions

Title pattern: `[<severity>] <alertname> — <namespace>/<pod>` (omit pod if absent)

Body (Markdown):
```
**Alert:** `<alertname>` (`<severity>`)
**Started:** `<startsAt>` (UTC)
**Namespace:** `<namespace>` &nbsp; **Pod:** `<pod>` &nbsp; **Node:** `<node>`

> <summary>

<description>

---
- [Prometheus rule](<generatorURL>)
- [Runbook](<runbook_url>) (if present)
- Fingerprint: `<fingerprint>`
```

### Labels applied to issues

- `incident` — top-level filter (matches our existing convention from #128 epic work)
- `severity:critical` / `severity:warning`
- `namespace:<ns>` — useful for routing/filtering
- `fingerprint:<sha>` — the dedup key
- `alertname:<value>` — convenient for human filtering by rule

## Test plan

Once everything is wired:

1. **Trigger a synthetic alert.** Either patch a deployment to crashloop briefly, or temporarily set a PrometheusRule with a 5s `for:` that you can flip back. Confirm an issue appears within a minute, labeled correctly.
2. **Re-fire.** Within the alert's repeat-interval, expect a comment on the existing issue, NOT a new issue.
3. **Resolve.** Once the alert clears, expect a "Resolved at …" comment + the issue auto-closes.
4. **Bootstrap edge case.** Restart n8n while an alert is firing; confirm Alertmanager's resolve event closes the issue cleanly.

## Future work (separate issues)

- **Auto-triage** (#127): when an issue is created with the `incident` label,
  n8n calls the kagent observability-agent for a first-pass analysis and posts
  it as a comment on the issue.
- **Slack thread linkage** — also include a deeplink to the existing Slack
  notification in `#incidents` so humans can correlate.
- **Severity escalation** — info-severity alerts could go to a lighter-weight
  destination (Slack only, no issue) which is the current behavior; revisit
  if any info alert turns out to need durable tracking.
