#!/usr/bin/env python3
"""Eval 01 runner — k8s-agent quality.

For each dataset item: invoke the agent via A2A, compute ground truth via
kubectl (for `computed` items), score correctness + tool-use, and push a
Langfuse dataset run (trace + scores) via the public REST API.

Reasoning (`judge`) items are logged as traces but not scored here — that's
M-eval-3 (Langfuse LLM-as-judge). See docs/eval-01-k8s-agent-quality.md.

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST, RUN_NAME (optional).
"""
import os, re, sys, json, base64, time, subprocess, urllib.request, urllib.error

_LF_DOWN = False  # set once if Langfuse is unreachable (e.g. parked/scaled to 0)

LF_HOST = os.environ.get("LANGFUSE_HOST", "http://langfuse-web.langfuse.svc.cluster.local:3000")
AUTH = "Basic " + base64.b64encode(
    f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()).decode()
A2A = "http://kagent-controller.kagent.svc.cluster.local:8083/api/a2a/kagent"
RUN = os.environ.get("RUN_NAME", "baseline-" + time.strftime("%Y%m%d-%H%M%S"))
NOW = lambda: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def lf(path, payload):
    # Best-effort: Langfuse storage is optional. If it's parked (scaled to 0) the
    # gate still runs — scores are computed regardless; we just skip storing.
    global _LF_DOWN
    if _LF_DOWN:
        return None
    req = urllib.request.Request(LF_HOST + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": AUTH}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = already exists (dataset/item) — fine
            print(f"  ! langfuse {path} -> {e.code}: {e.read().decode()[:160]}")
        return e.code
    except Exception:
        _LF_DOWN = True
        print("  (Langfuse unreachable — parked? skipping storage; scores/gate still computed)")
        return None


def ask_agent(agent, question):
    body = {"jsonrpc": "2.0", "id": "eval", "method": "message/send",
            "params": {"message": {"messageId": "m", "role": "user",
                                   "parts": [{"kind": "text", "text": question}]}}}
    req = urllib.request.Request(f"{A2A}/{agent}/", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-User-Id": "eval-runner@n2solutions.io"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)["result"]
    # Prefer the final artifact; some responses (esp. long reasoning) omit
    # `artifacts` and put the answer as the last agent text in `history`.
    arts = res.get("artifacts") or []
    answer = ""
    if arts and arts[0].get("parts"):
        answer = arts[0]["parts"][0].get("text", "")
    if not answer:
        for m in reversed(res.get("history", [])):
            if m.get("role") == "agent":
                for p in m.get("parts", []):
                    if p.get("kind") == "text" and p.get("text"):
                        answer = p["text"]
                        break
            if answer:
                break
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


JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-5-20250929")


def judge(question, answer, rubric):
    """LLM-as-judge: score a reasoning answer 0.0-1.0 against a rubric via the
    Anthropic Messages API. Returns (score, reason)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, "no ANTHROPIC_API_KEY"
    prompt = (
        "You are grading an AI agent's answer to a Kubernetes question. Score ONLY "
        "against the rubric — reward covering the rubric's points, penalize what it "
        "says to penalize. Be strict but fair.\n\n"
        f"QUESTION:\n{question}\n\nRUBRIC:\n{rubric}\n\nAGENT ANSWER:\n{answer}\n\n"
        'Respond with ONLY a JSON object: {"score": <float 0.0-1.0>, "reason": "<one sentence>"}')
    body = {"model": JUDGE_MODEL, "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = json.load(r)["content"][0]["text"]
    m = re.search(r'\{.*\}', text, re.S)
    obj = json.loads(m.group(0))
    return float(obj["score"]), obj.get("reason", "")


def main():
    ds = None
    import yaml
    ds = yaml.safe_load(open("/config/dataset-v1.yaml"))
    name = ds["dataset"]
    lf("/api/public/v2/datasets", {"name": name})
    default_agent = ds.get("agent", "k8s-agent")  # agent is dataset-level; items may override
    acc, toolrate, judged = [], [], []
    print(f"=== eval run {RUN} · dataset {name} · {len(ds['items'])} items ===")
    for it in ds["items"]:
        q, gt, expect_tool = it["input"], it["ground_truth"], it.get("expect_tool")
        try:
            answer, tools = ask_agent(it.get("agent", default_agent), q)
        except Exception as ex:
            print(f"[{it['id']}] AGENT ERROR: {ex}")
            continue
        expected, scores, jreason = None, {}, None
        if gt["type"] == "computed":
            expected = truth(gt["check"])
            scores["correct"] = score_match(answer, expected, gt["match"])
            acc.append(scores["correct"])
        elif gt["type"] == "judge":
            try:
                js, jreason = judge(q, answer, gt.get("rubric", ""))
                if js is not None:
                    scores["judge"] = js
                    judged.append(js)
            except Exception as ex:
                print(f"[{it['id']}] JUDGE ERROR: {ex}")
        if expect_tool:
            scores["tool_used"] = 1.0 if expect_tool in tools else 0.0
            toolrate.append(scores["tool_used"])
        tid = f"{it['id']}-{RUN}"
        lf("/api/public/dataset-items", {"datasetName": name, "id": it["id"],
            "input": {"question": q}, "expectedOutput": expected})
        lf("/api/public/ingestion", {"batch": [{"id": tid + "-t", "type": "trace-create",
            "timestamp": NOW(), "body": {"id": tid, "name": f"eval:{it['id']}", "input": q,
            "output": answer, "metadata": {"run": RUN, "expected": expected,
            "tools": sorted(tools), "judge_reason": jreason}}}]})
        if scores:
            lf("/api/public/ingestion", {"batch": [{"id": f"{tid}-{k}", "type": "score-create",
                "timestamp": NOW(), "body": {"traceId": tid, "name": k, "value": v}} for k, v in scores.items()]})
        lf("/api/public/dataset-run-items", {"runName": RUN, "datasetItemId": it["id"], "traceId": tid})
        tail = f" judge={jreason}" if jreason else ""
        print(f"[{it['id']}] ans={answer.strip()[:32]!r} exp={expected!r} tools={sorted(tools)} {scores}{tail}")

    a = sum(acc) / len(acc) if acc else None
    t = sum(toolrate) / len(toolrate) if toolrate else None
    j = sum(judged) / len(judged) if judged else None
    print(f"\n=== {RUN} summary ===")
    if a is not None:
        print(f"deterministic accuracy: {sum(acc):.0f}/{len(acc)} ({100*a:.0f}%)")
    if t is not None:
        print(f"tool-use rate:          {sum(toolrate):.0f}/{len(toolrate)} ({100*t:.0f}%)")
    if j is not None:
        print(f"llm-judge (reasoning):  mean {j:.2f} over {len(judged)} items")

    # Regression gate — only enforced when EVAL_GATE=1 (the scheduled gate sets it;
    # ad-hoc runs just report). Fails the run (exit 1) if any metric is below its
    # floor, which surfaces as a failed Job -> Alertmanager (see prometheusrule).
    if os.environ.get("EVAL_GATE") == "1":
        floors = {"accuracy": (a, float(os.environ.get("EVAL_MIN_ACCURACY", "0.7"))),
                  "tool-use": (t, float(os.environ.get("EVAL_MIN_TOOL", "0.8"))),
                  "judge":    (j, float(os.environ.get("EVAL_MIN_JUDGE", "0.5")))}
        breaches = [f"{k} {v:.2f} < {floor}" for k, (v, floor) in floors.items()
                    if v is not None and v < floor]
        if breaches:
            print("GATE: FAIL — " + "; ".join(breaches))
            sys.exit(1)
        print("GATE: PASS — all metrics at/above baseline floors")


if __name__ == "__main__":
    main()
