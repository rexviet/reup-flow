# Phase 00 gate results

Date: 2026-10-26. Verdict: **infra done; G1, G2, G3, G4, G6 pass (G2 accepted at 9/10 by the user); G5 blocked on labels. Gate not yet cleared.**

| Gate | Result | Note |
|---|---|---|
| G1 | **PASS** | Resume after SIGKILL proven with a scripted model and with two real OpenRouter models (`deepseek/deepseek-v4.1-flash`, `deepseek/deepseek-v4-pro`); both call tools |
| G2 | **PASS (accepted at 9/10 by the user, 2026-10-26)** | Run 2026-10-26 against a live VoiceStudio over a `trycloudflare` https tunnel. Auth, voice and TTS measured; STT timestamps good with `language=zh`; hand alignment 9/10 within 200 ms (1 fail, cause not yet confirmed). See "G2 run" below |
| G3 | **PASS** (with caveat) | One trace per run, Node span present, kill+resume adds no spans |
| G4 | **PASS** | Role isolation, schema unchanged, `count_workflows` works, Redis ACL derived and verified, spec green |
| G5 | **BLOCKED** | No OpenRouter key, and the sampled `data/` videos have no burned-in subtitles to label. Labels file is an empty template |
| G6 | **PASS** (synthetic transcript) | Max model-request input 62.5 KB realistic, 111.6 KB stress, limit 200 KB |

## Setup evidence

- Backup before any change: `backups/reup-20261026-093055.dump` (gitignored; 215 `videos` rows at the time).
- Image digests (all pinned in `docker-compose.yml`):
  - `temporalio/server:1.29@sha256:688da708d0daca6ff8083334a4a7fbbdbc7e70d1cca8e2eda8c05b21fdbd601e`
  - `temporalio/admin-tools:1.29@sha256:4a17b5be706c56a9238fb7b5b32a36966cee14939658b20597837b2443b277c8`
  - `temporalio/ui:2.40.0@sha256:a98ed66e330cc5b6b432fe174d97dee47d745fa72a1938cf1f6b781dfc335477`
  - `langfuse/langfuse:3@sha256:a343f64e035eb01aeea358703a0428945d909d01e19452509a5a830862dda878`
  - `langfuse/langfuse-worker:3@sha256:8a28c946bb5401eef488153fa294db5a79bd99dd5c90db8e4d39559374c9ebd3`
  - `clickhouse/clickhouse-server:24.8@sha256:1ffa82edee000a42c09313bd9f1293d94c570aee74babc1b3ca9983a35fa597b`
  - `cgr.dev/chainguard/minio@sha256:268d68a2a3b6fc65f905a61f84d7eab6345ce6bff14f551e2b028ec31e6a1341`
  - `postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36` (for `db-init` only)
- `docker compose ps`: every long-running service healthy; `db-init`, `temporal-admin-tools`, `temporal-namespace-init`, `minio-init` exit 0 and re-run to exit 0 (idempotent).
- Published ports on new services: `127.0.0.1:8233` (temporal-ui), `127.0.0.1:3100` (langfuse-web), `127.0.0.1:9001` (MinIO console). Nothing else.
- `temporal` namespace `default` retention: 30 days. `work/` lifecycle: 30 days; none on `cache/`, `review/`, `output/`.

## G1: durable agent

Reproduce: `./run.sh uv run python g1_durable_agent.py [--api durability|temporal-agent]` (from `services/localization/spikes`, stack up).
Worker `SIGKILL`ed right after the tool's `ActivityTaskCompleted`, then restarted.

| Check | `TemporalDurability` | `TemporalAgent` |
|---|---|---|
| each `call_no` has one row, `hits=1` | PASS | PASS |
| model turn 1 invoked once across the kill (pid differs after restart) | PASS | PASS |
| tool activities all on `io` | PASS | PASS (after fixing my toolset key to `<agent>`) |
| model activities all on `llm` | PASS | PASS |

Findings that change later phases:
1. **`TemporalAgent` is deprecated in pydantic-ai 2.61 (removal planned v3).** Phases 05/06 should use `Agent(..., capabilities=[TemporalDurability(...)])`. Both pass here.
2. Per-tool queue pinning: `tool_plain(fn, metadata={"temporal": ActivityConfig(task_queue="io", ...)})`. Legacy form: `tool_activity_config={"<agent>": {tool: cfg}}`.
3. `PydanticAIPlugin` registers agent activities only on the worker that hosts the workflow. Workers for `llm`/`io` need `AgentPlugin(agent)`.
4. Provider leg (run 2026-10-26): both models called the tool in a plain agent (`g1_durable_agent.py openrouter`) and through the durable workflow with a SIGKILL after the first tool completed (`--real <model>`). Each `call_no` ran once, tool activities on `io`, model activities on `llm`, history `model_request, call_tool, model_request, call_tool, model_request`. Caveat: the real-model run is one sample each and the model's calls are nondeterministic, so the checks assert invariants, not an exact sequence.

## G3: one trace, Node span, no replay spans

Reproduce: `./run.sh uv run python g3_trace.py`. Run A uninterrupted; runs B1–B3 kill the Python worker after the first activity.
- Exactly the 4 expected traces existed in the window. `RunActivity:render` (service `render-worker`) is in the same trace as the Python spans in every run.
- Kill+resume **added no spans** in any B run (Python-side local log and Langfuse both agree).
- Caveat: spans that ended in the ~100 ms before the kill are lost permanently (replay suppresses them), so resumed traces can lack e.g. `RunWorkflow`. Never duplicated.
- Two pitfalls found and fixed in the spike, to carry into phase 04:
  - Use `BatchSpanProcessor`. With `SimpleSpanProcessor`, workflow-side spans export inside the sandbox thread and are dropped ("Cannot access `__builtins__.open` from inside a workflow").
  - Langfuse ingests in delayed batches. Count only after span totals are stable across polls, otherwise a half-ingested trace looks like missing spans.

## G4: isolation

- `pg_dump --schema-only reup` before vs after: empty diff (after stripping pg_dump's random `\restrict`/`\unrestrict` nonce lines).
- Roles `temporal`, `langfuse`, `localization`: not superuser, no createdb. `langfuse`/`temporal` → `reup`: `permission denied for database "reup"`; each cannot connect to the other's database. `localization` can connect to `reup` but `select from videos` is denied (grants come in phases 02/03).
- `count_workflows('ExecutionStatus="Running"')` works on SQL visibility.
- Redis: no fixed prefix, but Langfuse only uses `bull:*`, `langfuse:*`, `api-key:*` (from a `MONITOR` capture of real ingestion). ACL tightened to those. Verified: `langfuse` gets `NOPERM` on a `douyin:*` key, `FLUSHALL` and `KEYS`; `default` is unchanged; no `NOPERM` in Langfuse logs; `maxmemory-policy noeviction`. Separate Redis was not needed.
- `compose-constraints.spec.ts`: 17 tests green (11 new). Mutation harness `phase-infra.sh`: 11/11 caught, 2/2 controls green, registered in `scripts/run-mutations.sh` as `infra:11:2`. The first version printed success while a bash 3.2 error meant nothing had run; it now counts its own outcomes.
- Not covered: pre-existing `postgres:5432`, `redis:6379` still publish on all interfaces (unchanged, legacy).

## G6: history size

Reproduce: `./run.sh uv run python g6_history_size.py`. Transcript is synthetic (3 min, per-character word timestamps, 40 KB realistic / 78 KB stress), tool and model activities are real, sizes read from Temporal history.
Max model-request input: 62,553 B realistic, 111,601 B stress, under 204,800 B. Phase 05 does not need 60-second windows, but re-measure with a real transcript.

## G2 run (2026-10-26, live VoiceStudio)

URL class: `https/hostname` (a temporary `trycloudflare` tunnel; fine for the gate, not for a long-lived `VOICESTUDIO_URL`).

| Check | Result |
|---|---|
| Request without a key | **401** `API key required`. Pass. (An earlier tunnel host answered 403 on host-name allow-listing before auth was reached.) |
| `GET /v1/audio/voices` contains `VOICE_ID` | `demo0001` exists (type `profile`, language English). The other 13 entries are OpenAI aliases (`alloy`, `nova`, ...) that map silently to the active engine's default voice. Pass for the check, but see findings |
| `POST /v1/audio/speech` (`tts-1`, `demo0001`) | 200, measurable. Sentence of 12 syllables read in 3.15 s = **3.81 syllables/s** (English profile voice reading Vietnamese, so not yet a usable rate for D6) |
| STT, default language (`whisper-1`) | Unreliable: language auto-detect returned Sinhala for one clip and French for another. Fails as a Chinese transcriber without a language hint |
| STT, `language=zh` | 3 of 3 clips with speech returned word timestamps for every character: **missing-word ratio 0.0** (measured as text chars vs joined word chars), 201-253 chars per 45 s clip |
| Clips with no usable result | 5 of 11 tried returned an empty transcript with `duration: 0.0` (music or no speech, not verified). `missing_word_ratio` returns 0.0 for an empty text, so the spike's own metric reads empty clips as perfect |

Findings that change later phases:
1. The transcription request MUST pass `language` (`zh` for source video). Phase 04 `transcribe` and the `SttProvider` settings need a language field.
2. `g2_voicestudio.py` must count an empty transcript as a failure, not 0.0 missing. Not yet fixed.
3. `STT_MODEL` and `TTS_MODEL` are OpenAI aliases for whichever engine is active; the response carries no `engine` field (`engine: null`), so the engine that feeds the cache key (plan.md §0.4) cannot be read from the response. Needs an explicit way to record it (config value, or a server endpoint if one exists).
4. There is no Vietnamese voice on this server yet; D6 speech rate (phase 05) and the dubbing phases need one. Unknown voice ids fall back silently, so `VOICE_ID` must be checked against `/voices` before every run.
5. Hand alignment, done by the user by ear (EXACT window vs the window widened by 200 ms): **9 of 10 sentences pass**. Sentence #10 (clip ending 94878780, 35.67 s to 43.63 s) fails: the 200 ms-padded clip still cuts the tail. That window is 7.96 s, right at the 8 s limit the sheet builder splits at, so the cut may come from the builder splitting a sentence mid-phrase rather than from the server's timestamp. Not confirmed: the tunnel went away before the words around 43.6 s could be inspected.
6. The user listened to the two clips the server returned empty for: music only, so empty is the correct answer.
7. **Decision (user, 2026-10-26): accept 9/10 as G2 pass.** The one failure is unexplained and may come from the sheet builder splitting a sentence at its 8 s limit; it was not re-checked. Carry-overs that still bind later phases: transcription must send `language`; the active engine name is not in the response; no Vietnamese voice exists on the server yet.
8. Automated STT re-run with `language=zh` and empty-as-failure: 5 clips with speech measured, missing-word ratio 0.0 on all 5.

Reproduce: `VOICE_ID=demo0001 STT_MODEL=whisper-1 TTS_MODEL=tts-1 ./run.sh uv run python g2_voicestudio.py` with `VOICESTUDIO_URL` and `VOICESTUDIO_API_KEY` in the environment (kept in the gitignored `.env`).

## Deviations from the phase file

- Added `temporal-namespace-init` (the plain server image creates no namespace) and `infra/temporal/*.sh`, `infra/clickhouse/low-memory.xml`, `infra/minio/Dockerfile` (the official `mc` image is not anonymously pullable and the Chainguard one has no shell).
- `temporal-admin-tools` is a one-shot schema job, not a long-lived tools container.
- Spikes run inside the compose network (`spikes/run.sh`), because Temporal, Postgres and Langfuse publish no host port.

## To unblock

1. G5 needs `OCR_BBOX_FORMAT` (one of `xyxy_px`, `xyxy_norm1`, `xyxy_norm1000`, `yxyx_norm1000`) once labels exist.
2. G2: hand-align 10 sentences (≤ 200 ms), add a Vietnamese voice on the VoiceStudio host, and decide how to read the active engine name.
3. Provide 5 videos with burned-in Chinese subtitles and hand-label 20 frames in `spikes/fixtures/g5-labels.json`.
