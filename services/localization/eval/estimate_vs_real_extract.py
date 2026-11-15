"""Step 1 of the estimate-versus-real comparison (host): real TTS durations, the voice rate
and the script keys of past runs.

    uv run python eval/estimate_vs_real_extract.py clips.json
    docker exec -i reup-flow-worker-media-1 python -c "$(cat eval/estimate_vs_real_compare.py)" < clips.json"""
import base64, json, sys
import httpx
sys.path.insert(0, "eval")
from common import History, TEMPORAL_UI

def dec(field):
    return [json.loads(base64.b64decode(p["data"])) if "data" in p else None for p in (field or {}).get("payloads", [])]

ui = TEMPORAL_UI
listing, token = [], ""
while True:
    r = httpx.get(f"{ui}/api/v1/namespaces/default/workflows",
                  params={"query": 'WorkflowId STARTS_WITH "video-"', "pageSize": 100, "nextPageToken": token}, timeout=30).json()
    listing += r.get("executions", [])
    token = r.get("nextPageToken") or ""
    if not token: break
out = {}
for ex in listing:
    wid = ex["execution"]["workflowId"]
    try:
        h = History.fetch(ui, wid)
    except Exception as e:  # noqa: BLE001
        print("skip", wid, type(e).__name__, file=sys.stderr); continue
    sched = {}
    rec = {"clips": {}, "script_keys": set(), "rate": None, "voice": None, "aweme": wid.split("-")[1]}
    for e in h.events:
        a = e.get("activityTaskScheduledEventAttributes")
        if a: sched[int(e["eventId"])] = (a["activityType"]["name"], dec(a.get("input")))
        c = e.get("activityTaskCompletedEventAttributes")
        if not c: continue
        name, inp = sched[int(c["scheduledEventId"])]
        res = dec(c.get("result"))
        if name == "localizer_context" and inp and isinstance(inp[0], dict):
            rec["rate"] = inp[0].get("syllables_per_sec")
        if name == "synthesize_segments" and inp:
            rec["voice"] = inp[0].get("voice")
            for batch in (res[0] or []) if res and res[0] else []:
                for clip in batch if isinstance(batch, list) else [batch]:
                    if isinstance(clip, dict) and "hash" in clip:
                        rec["clips"][clip["hash"]] = clip["duration_ms"]
            if len(inp) > 2 and isinstance(inp[2], str): rec["script_keys"].add(inp[2])
        if name == "assemble_script" and res and isinstance(res[0], dict):
            k = (res[0].get("script") or {}).get("script_key")
            if k: rec["script_keys"].add(k)
    if rec["clips"] and rec["rate"] and rec["voice"]:
        rec["script_keys"] = sorted(rec["script_keys"]); out[wid] = rec
print(json.dumps(out), file=open(sys.argv[1], "w"))
print({w: (r["voice"]["provider"], r["voice"]["voice_id"], round(r["rate"],2), len(r["clips"])) for w, r in out.items()})
