# Phase 00 gate results

Date: 2026-10-26. Verdict: **infra done; G1, G3, G4, G6 pass; G2 and G5 blocked on user inputs. Gate not yet cleared.**

| Gate | Result | Note |
|---|---|---|
| G1 | **PASS** | Resume after SIGKILL proven with a scripted model and with two real OpenRouter models (`deepseek/deepseek-v4.1-flash`, `deepseek/deepseek-v4-pro`); both call tools |
| G2 | **BLOCKED** | No `VOICESTUDIO_URL`. Script ready, selftest green |
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

## Deviations from the phase file

- Added `temporal-namespace-init` (the plain server image creates no namespace) and `infra/temporal/*.sh`, `infra/clickhouse/low-memory.xml`, `infra/minio/Dockerfile` (the official `mc` image is not anonymously pullable and the Chainguard one has no shell).
- `temporal-admin-tools` is a one-shot schema job, not a long-lived tools container.
- Spikes run inside the compose network (`spikes/run.sh`), because Temporal, Postgres and Langfuse publish no host port.

## To unblock

1. G5 needs `OCR_BBOX_FORMAT` (one of `xyxy_px`, `xyxy_norm1`, `xyxy_norm1000`, `yxyx_norm1000`) once labels exist.
2. Set `VOICESTUDIO_URL`, `VOICESTUDIO_API_KEY`, `VOICE_ID`, `STT_MODEL`, `TTS_MODEL`, enable `OMNIVOICE_API_KEY` on that host → run G2, then hand-align 10 sentences.
3. Provide 5 videos with burned-in Chinese subtitles and hand-label 20 frames in `spikes/fixtures/g5-labels.json`.
