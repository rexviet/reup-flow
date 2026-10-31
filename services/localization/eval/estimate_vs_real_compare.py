"""Step 2 (runs in worker-media, which reaches the object store): the syllable estimate
against the real TTS duration of each clip.

Only lines spoken as one clip whose text equals a script line are matched. That leaves out the
lines rewritten after the first draft, so the sample leans towards lines the estimate got right."""
import base64, json, statistics as st, sys
from localization.config import Settings
from localization.contracts.models import STRETCH_MAX
from localization.storage.object_store import ObjectStore
from localization.text.vi_syllables import count_syllables, estimate_ms

store = ObjectStore.from_settings(Settings())
data = json.load(sys.stdin)
scripts: dict[str, dict] = {}
def script(key):
    if key not in scripts:
        try: scripts[key] = json.loads(store.get_bytes(key, produced_by="estimate-compare"))
        except Exception: scripts[key] = {"segments": []}
    return scripts[key]

import re
loose = lambda t: re.sub(r'[\W_]+', '', t).casefold()
rows, seen, unmatched, nometa = [], set(), 0, 0
for wid, rec in data.items():
    rate = rec["rate"]
    slots = {}
    for key in rec["script_keys"]:
        for seg in script(key).get("segments", []):
            slots.setdefault(loose(seg["text_vi"]), (seg["slot_ms"], seg["text_vi"].strip()))
    for h, hist_ms in rec["clips"].items():
        if (h, rate) in seen: continue
        seen.add((h, rate))
        meta = store.metadata(f"cache/tts/{h}.wav")
        if not meta or "text-vi" not in meta: nometa += 1; continue
        text = base64.b64decode(meta["text-vi"]).decode().strip()
        real = int(meta.get("duration-ms", hist_ms))
        hit = slots.get(loose(text))
        if hit is None: unmatched += 1; continue
        slot, text = hit
        est = estimate_ms(text, rate)
        rows.append({"wid": wid, "chars": len(text), "voice": rec["voice"]["voice_id"], "rate": rate, "syl": count_syllables(text),
                     "slot": slot, "est": est, "real": real})

def pct(xs, p): xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))]
def summarise(rs):
    ratio = [r["real"] / r["est"] for r in rs if r["est"]]
    ape = [abs(r["real"] - r["est"]) / r["real"] for r in rs]
    fit = lambda ms, r: ms <= r["slot"] * STRETCH_MAX
    both_fit = sum(1 for r in rs if fit(r["est"], r) and fit(r["real"], r))
    both_unfit = sum(1 for r in rs if not fit(r["est"], r) and not fit(r["real"], r))
    est_fit_real_unfit = sum(1 for r in rs if fit(r["est"], r) and not fit(r["real"], r))
    est_unfit_real_fit = sum(1 for r in rs if not fit(r["est"], r) and fit(r["real"], r))
    over = [r["real"] / (r["slot"] * STRETCH_MAX) for r in rs if fit(r["est"], r) and not fit(r["real"], r)]
    return {
        "clips": len(rs), "real_over_est_median": round(st.median(ratio), 3),
        "real_over_est_p10_p90": [round(pct(ratio, .1), 3), round(pct(ratio, .9), 3)],
        "mean_abs_pct_error": round(100 * st.fmean(ape), 1),
        "est_says_fit_and_real_fits": both_fit, "both_unfit": both_unfit,
        "est_says_fit_but_real_overruns": est_fit_real_unfit,
        "est_says_unfit_but_real_fits": est_unfit_real_fit,
        "overrun_when_missed_median_x_limit": round(st.median(over), 2) if over else None,
    }
import collections
buckets = collections.Counter(("<=80 chars" if r["chars"] <= 80 else "81-160" if r["chars"] <= 160 else ">160") for r in rows)
out = {"length_buckets": dict(buckets), "stretch_max": STRETCH_MAX, "unmatched_text": unmatched, "no_metadata": nometa, "all": summarise(rows)}
cur = [r for r in rows if r["voice"] == "4e015cab"]
if cur: out["current_voice_4e015cab"] = summarise(cur)
by_voice = {}
for r in rows: by_voice.setdefault(r["voice"], []).append(r)
out["by_voice"] = {v: {"rate": rs[0]["rate"], **summarise(rs)} for v, rs in by_voice.items() if len(rs) >= 8}
print(json.dumps(out, indent=1))
json.dump(rows, open("/tmp/estimate_rows.json", "w"))
