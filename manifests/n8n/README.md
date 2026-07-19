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
under `alertmanager.config.receivers.n8n-incidents`. n8n's GitHub node
authenticates via a **GitHub OAuth2** credential created in the n8n UI and
authorized as the dedicated `n2solutions-bot` machine account (see setup below) —
there is no GitHub secret synced into the cluster.

## One-time setup

### 1. GitHub: dedicated bot account + OAuth app

Auth uses a **GitHub OAuth2** credential (self-refreshing) authorized as a
dedicated machine account — no personal PAT, no static secret synced into the
cluster. n8n Community stores the credential in its own encrypted DB (the
"External Secrets" integration is Enterprise-only), so there is **nothing to
provision via OpenBao/ExternalSecrets** for GitHub.

- **Machine account:** `n2solutions-bot` — an **outside collaborator** on the
  `n2solutionsio` org (not a member, so no org-wide repo access). 2FA enabled;
  login + TOTP in 1Password (`homelab` vault).
- **Repo grant (least privilege):** add `n2solutions-bot` to
  `n2solutionsio/homelab-gitops` **only**, with the **Triage** role — enough to
  create / comment / close / label issues, without code write access.
- **OAuth app:** `n2solutions-n8n-homelab`, **org-owned**
  (`n2solutionsio` → Settings → Developer settings → OAuth Apps). Callback URL:
  `https://n8n.homelab.n2solutions.io/rest/oauth2-credential/callback`.
  Client ID + Secret in 1Password.

### 2. n8n: create the GitHub OAuth2 credential

Log into GitHub as `n2solutions-bot` (separate/incognito browser), then in the
n8n UI (`https://n8n.homelab.n2solutions.io`):
1. **Credentials → New → GitHub OAuth2 API** (the OAuth2 variant, **not** "GitHub API")
2. Paste the OAuth app's **Client ID** and **Client Secret** (from 1Password)
3. Confirm the displayed **OAuth Redirect URL** matches the OAuth app's callback
   exactly (`.../rest/oauth2-credential/callback`)
4. Click **Connect my account** → authorize as `n2solutions-bot` → grant `n2solutionsio`
5. Save as `GitHub - homelab-gitops (bot)`

n8n stores and auto-refreshes the token — no rotation toil, no PAT, nothing in OpenBao.

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

## Auto-triage extension (#127)

After the base alert-incident pipeline above is working, extend the **same n8n
workflow** to call the kagent observability-agent and post its first-pass
analysis as the opening comment on the issue. We're not building a separate
workflow — chaining via GitHub webhooks adds moving parts and creates loop-back
risk. One workflow does the whole `alert → issue → first triage` chain.

### Where the new nodes go

Insert AFTER the GitHub "create issue" node (5a in the table above), and BEFORE
the workflow ends. So the flow becomes:

```
Webhook → Code(parse) → Search Existing → IF
  ├─ (firing + no issue) → Create Issue → [NEW: Call kagent → Post Comment]
  ├─ (firing + existing issue) → Add "re-fire" comment
  └─ (resolved + existing issue) → Close
```

### Pre-flight check

The kagent observability-agent A2A endpoint must respond. Verified working:

```bash
kubectl run a2a-probe --rm -i --restart=Never --image=curlimages/curl:8.7.1 -- \
  curl -sw "\nHTTP %{http_code}\n" \
  -X POST -H 'Content-Type: application/json' -H 'X-User-Id: n8n@n2solutions.io' \
  http://kagent-controller.kagent.svc.cluster.local:8083/api/a2a/kagent/observability-agent/ \
  -d '{
    "jsonrpc":"2.0","id":"probe","method":"message/send",
    "params":{"message":{"messageId":"m1","role":"user",
      "parts":[{"kind":"text","text":"How many pods in kagent namespace?"}]}}
  }'
```

Returns a JSON-RPC response where the agent's final answer is at
`result.artifacts[0].parts[0].text`. Full tool-use trace is in `result.history`.

### Auto-triage nodes (per-step)

| # | Node | Purpose |
| --- | --- | --- |
| T1 | **IF — should-triage?** | Skip if any of: alert severity is `info`; the firing-firing repeat path (5b); existing issue already has label `triaged`. Cheap loop-prevention. |
| T2 | **Code — build investigation prompt** | Compose the prompt from the alert payload (see template below). Keep it short — long prompts blow up the agent's input token bill. |
| T3 | **HTTP Request — call observability-agent** | POST to the A2A endpoint above. Timeout 120s. Wait for response, don't stream (kagent#837 has a streaming bug). |
| T4 | **Code — extract response** | Pull `result.artifacts[0].parts[0].text` as the analysis. If the response has `status.state !== "completed"` or an `error`, return a fallback message instead. |
| T5 | **GitHub — Create Issue Comment** | Post the analysis as the first comment on the issue created in 5a. Include token-usage line at the bottom (`result.metadata.kagent_usage_metadata.totalTokenCount`). |
| T6 | **GitHub — Add Label** | Apply `triaged` label so we don't re-investigate on every re-fire. |

### Prompt template (T2)

```js
const a = $json;  // normalized alert from the parse Code node
return [{
  json: {
    prompt: `You are an SRE investigating an active alert in the homelab k3s cluster.
ALERT: ${a.alertname} (severity=${a.severity})
NAMESPACE: ${a.namespace}${a.pod ? `\nPOD: ${a.pod}` : ''}
STARTED: ${a.startsAt}
SUMMARY: ${a.summary || '(none)'}
DESCRIPTION: ${a.description || '(none)'}

Investigate using your PromQL, kubectl, and log-query tools. Be concise:
1. What's the actual problem (one sentence)?
2. What recent change or condition triggered it (one sentence + evidence)?
3. What action should the human take next (one sentence)?

Keep the entire response under 400 words. Do NOT speculate beyond what the tools show.`,
    messageId: `triage-${a.fingerprint}`,
  }
}];
```

### A2A request body (T3)

```json
{
  "jsonrpc": "2.0",
  "id": "{{ $json.messageId }}",
  "method": "message/send",
  "params": {
    "message": {
      "messageId": "{{ $json.messageId }}",
      "role": "user",
      "parts": [{ "kind": "text", "text": "{{ $json.prompt }}" }]
    }
  }
}
```

Headers:
- `Content-Type: application/json`
- `X-User-Id: n8n@n2solutions.io` (kagent auth.mode=unsecure trusts this)

### Response extraction (T4)

```js
const r = $json.result;
if (!r || r.status?.state !== 'completed') {
  return [{ json: {
    comment: `> **Auto-triage failed** — agent did not return a completed task. ` +
             `Investigate manually. (Raw error: \`${JSON.stringify($json.error || r?.status)}\`)`
  }}];
}
const analysis = r.artifacts?.[0]?.parts?.[0]?.text || '(empty response)';
const tokens = r.metadata?.kagent_usage_metadata?.totalTokenCount || 'unknown';
return [{ json: {
  comment: `## 🤖 observability-agent — first-pass triage\n\n${analysis}\n\n---\n` +
           `<sub>Tokens: ${tokens} · ` +
           `[Re-run](kagent.homelab.n2solutions.io) by adding a comment</sub>`
}}];
```

### Cost protection

The bare facts:
- A short investigation runs **~10k–15k tokens** on the observability-agent (the probe above used 11,270 tokens for a one-word answer). Complex prompts can hit 30k+.
- At Anthropic Sonnet pricing (input $3/M, output $15/M), that's ~$0.04 per triage. Cheap individually; matters at scale.

Hard guards:
1. **Skip `severity=info`** in T1. The bulk of CPUThrottlingHigh-style alerts shouldn't auto-triage.
2. **`triaged` label gate** — once an issue is triaged, never re-triage on re-fires. The human can manually unlabel and add a "retriage" comment to re-run.
3. **Per-day cap (optional, but recommended for production)** — n8n's static-data feature can count invocations; refuse after N/day. For homelab, the severity gate is usually enough.
4. **Severity-specific timeouts** — give critical alerts longer (e.g. 180s), warning alerts shorter (60s). Prevents one slow agent run from blocking the workflow.

### Failure modes to handle gracefully (in T4 or T5)

| Failure | Comment to post |
| --- | --- |
| Agent timeout / HTTP error | "Auto-triage failed (agent unreachable). Investigate manually." |
| Agent returns `status.state="failed"` | "Auto-triage failed (agent error). Raw response in workflow logs." |
| Agent returns empty text | "Auto-triage returned no findings. May need a more specific prompt." |
| Loop detected (already triaged) | (skip entirely via T1 IF node) |

Always post *something* so the human knows triage was attempted and didn't silently succeed.

### Test plan (auto-triage portion)

Once the auto-triage nodes are added to the workflow:

1. **Synthetic firing alert** (severity=warning) → issue appears with the kagent analysis as the first comment within ~2 min.
2. **Re-fire the same alert** → "re-fired" comment posted, NO new triage comment, `triaged` label already present.
3. **Severity=info alert** → issue created (or not, depending on routing), NO triage comment.
4. **Kill the observability-agent pod mid-workflow** → fallback "auto-triage failed" comment posted, workflow doesn't crash.

## Future work (separate issues)

- **Slack thread linkage** — also include a deeplink to the existing Slack
  notification in `#incidents` so humans can correlate.
- **Severity escalation** — info-severity alerts could go to a lighter-weight
  destination (Slack only, no issue) which is the current behavior; revisit
  if any info alert turns out to need durable tracking.
- **Specialized agent routing** — for some alert classes, a non-default agent
  is a better fit (CiliumNetworkPolicy-related → `cilium-debug-agent`; kagent
  pod issues → `k8s-agent`). Add a Switch node before T3 that picks the agent
  by alert labels.
- **Multi-turn investigation** — currently single-shot. Could re-invoke the
  agent if the first response doesn't include a concrete next action. Costs
  more tokens; defer until we see the failure pattern.
