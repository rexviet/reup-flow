"""Of the lines the Localizer passed (assess clean on the estimate), how many did the real
measure (`measure_fit`) flag as too long?

    uv run python eval/measure_fit_miss_rate.py

Reads the histories of past runs from the Temporal UI API. A line the Localizer itself marked
as running over also counts, so the rate is an upper bound on what the estimate misses."""
import base64, json, sys, httpx
sys.path.insert(0, "eval")
from common import History, TEMPORAL_UI
def dec(f): return [json.loads(base64.b64decode(p["data"])) if "data" in p else None for p in (f or {}).get("payloads", [])]
listing, token = [], ""
while True:
    r = httpx.get(f"{TEMPORAL_UI}/api/v1/namespaces/default/workflows", params={"query": 'WorkflowId STARTS_WITH "video-"', "pageSize": 100, "nextPageToken": token}, timeout=30).json()
    listing += r.get("executions", []); token = r.get("nextPageToken") or ""
    if not token: break
rows = []
for ex in listing:
    wid = ex["execution"]["workflowId"]
    h = History.fetch(TEMPORAL_UI, wid)
    sched, lines_of_script, provider, first_measure_done = {}, {}, None, set()
    for e in h.events:
        a = e.get("activityTaskScheduledEventAttributes")
        if a: sched[int(e["eventId"])] = (a["activityType"]["name"], dec(a.get("input")))
        c = e.get("activityTaskCompletedEventAttributes")
        if not c: continue
        name, inp = sched[int(c["scheduledEventId"])]; res = dec(c.get("result"))
        if name == "assemble_script" and res and isinstance(res[0], dict):
            k = (res[0].get("script") or {}).get("script_key")
            n = len((inp[3] or {}).get("lines", [])) if len(inp) > 3 and isinstance(inp[3], dict) else None
            if k and n: lines_of_script[k] = n
            if inp and isinstance(inp[0], dict): provider = (inp[0].get("voice") or {}).get("provider")
        if name == "measure_fit" and res and isinstance(res[0], dict) and len(inp) > 1 and isinstance(inp[1], str):
            key = inp[1]
            if key in lines_of_script and key not in first_measure_done and ".v1." in key or (key in lines_of_script and key not in first_measure_done and key.endswith(".v3.json")):
                first_measure_done.add(key)
                rows.append({"wid": wid, "provider": provider, "script": key.rsplit("/", 1)[-1], "lines": lines_of_script[key], "unfit": len(res[0].get("unfit_ids", []))})
first = [r for r in rows if r["script"] == "script.v1.json"]
def rate(rs): 
    t = sum(r["lines"] for r in rs); b = sum(r["unfit"] for r in rs)
    return f"runs {len(rs)}, lines {t}, unfit {b}, {100*b/t:.1f}%, runs with an unfit line {sum(1 for r in rs if r['unfit'])}"
print("ALL first measures        :", rate(first))
latest = {}
for r in first:
    v, g = r["wid"].rsplit("-g", 1)
    if v not in latest or int(g) > int(latest[v]["wid"].rsplit("-g", 1)[1]): latest[v] = r
print("LATEST run per video      :", rate(list(latest.values())))
for v, r in sorted(latest.items()): print("   ", v[-8:], r["wid"].rsplit("-", 1)[1], "lines", r["lines"], "unfit", r["unfit"])
cur = [r for r in first if r["wid"].endswith(("7616569292510948709-g9", "7618513827751629177-g9"))]
print("current voice (g9) runs    :", [(r["wid"][-6:], r["lines"], r["unfit"]) for r in cur])
