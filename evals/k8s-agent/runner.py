#!/usr/bin/env python3
"""Eval 01 runner — k8s-agent quality.

For each dataset item: invoke the agent via A2A, compute ground truth via
kubectl (for `computed` items), score correctness + tool-use, and push a
Langfuse dataset run (trace + scores) via the public REST API.

Reasoning (`judge`) items are logged as traces but not scored here — that's
M-eval-3 (Langfuse LLM-as-judge). See docs/eval-01-k8s-agent-quality.md.

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST, RUN_NAME (optional).
"""
import os, re, json, base64, time, subprocess, urllib.request, urllib.error

LF_HOST = os.environ.get("LANGFUSE_HOST", "http://langfuse-web.langfuse.svc.cluster.local:3000")
AUTH = "Basic " + base64.b64encode(
    f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()).decode()
A2A = "http://kagent-controller.kagent.svc.cluster.local:8083/api/a2a/kagent"
RUN = os.environ.get("RUN_NAME", "baseline-" + time.strftime("%Y%m%d-%H%M%S"))
NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def lf(path, payload):
    req = urllib.request.Request(LF_HOST + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": AUTH}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = already exists (dataset/item) — fine
            print(f"  ! langfuse {path} -> {e.code}: {e.read().decode()[:160]}")
        return e.code


def ask_agent(agent, question):
    body = {"jsonrpc": "2.0", "id": "eval", "method": "message/send",
            "params": {"message": {"messageId": "m", "role": "user",
                                   "parts": [{"kind": "text", "text": question}]}}}
    req = urllib.request.Request(f"{A2A}/{agent}/", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-User-Id": "eval-runner@n2solutions.io"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)["result"]
    answer = res["artifacts"][0]["parts"][0]["text"]
    tools = set(re.findall(r'"name":\s*"([a-z0-9_]+)"', json.dumps(res.get("history", []))))
    return answer, tools


def truth(check):
    return subprocess.run(["sh", "-c", check], capture_output=True, text=True, timeout=40).stdout.strip()


def score_match(answer, expected, mode):
    a, e = answer.strip().lower(), expected.strip().lower()
    if mode == "numeric":
        nums = re.findall(r'-?\d+', a)
        return 1.0 if (nums and e in nums) else 0.0
    if mode == "exact":
        return 1.0 if a == e else 0.0
    if mode == "contains":
        return 1.0 if e and e in a else 0.0
    return 0.0


def main():
    ds = None
    import yaml
    ds = yaml.safe_load(open("/config/dataset-v1.yaml"))
    name = ds["dataset"]
    lf("/api/public/v2/datasets", {"name": name})
    acc, toolrate = [], []
    print(f"=== eval run {RUN} · dataset {name} · {len(ds['items'])} items ===")
    for it in ds["items"]:
        q, gt, expect_tool = it["input"], it["ground_truth"], it.get("expect_tool")
        try:
            answer, tools = ask_agent(it["agent"], q)
        except Exception as ex:
            print(f"[{it['id']}] AGENT ERROR: {ex}")
            continue
        expected, scores = None, {}
        if gt["type"] == "computed":
            expected = truth(gt["check"])
            scores["correct"] = score_match(answer, expected, gt["match"])
            acc.append(scores["correct"])
        if expect_tool:
            scores["tool_used"] = 1.0 if expect_tool in tools else 0.0
            toolrate.append(scores["tool_used"])
        tid = f"{it['id']}-{RUN}"
        lf("/api/public/dataset-items", {"datasetName": name, "id": it["id"],
            "input": {"question": q}, "expectedOutput": expected})
        lf("/api/public/ingestion", {"batch": [{"id": tid + "-t", "type": "trace-create",
            "timestamp": NOW(), "body": {"id": tid, "name": f"eval:{it['id']}", "input": q,
            "output": answer, "metadata": {"run": RUN, "expected": expected, "tools": sorted(tools)}}}]})
        if scores:
            lf("/api/public/ingestion", {"batch": [{"id": f"{tid}-{k}", "type": "score-create",
                "timestamp": NOW(), "body": {"traceId": tid, "name": k, "value": v}} for k, v in scores.items()]})
        lf("/api/public/dataset-run-items", {"runName": RUN, "datasetItemId": it["id"], "traceId": tid})
        print(f"[{it['id']}] ans={answer.strip()[:32]!r} exp={expected!r} tools={sorted(tools)} {scores}")

    print(f"\n=== {RUN} summary ===")
    if acc:
        print(f"deterministic accuracy: {sum(acc):.0f}/{len(acc)} ({100*sum(acc)/len(acc):.0f}%)")
    if toolrate:
        print(f"tool-use rate:          {sum(toolrate):.0f}/{len(toolrate)} ({100*sum(toolrate)/len(toolrate):.0f}%)")
    print(f"judge items logged (unscored, see M-eval-3): {sum(1 for i in ds['items'] if i['ground_truth']['type']=='judge')}")


if __name__ == "__main__":
    main()
