# Agent Workspace

A page in the web app that shows which standard-pipeline (VoiceStudio) localization
agent is working on which video, from real data. Vbee has no dedicated desks here.

**Open it:** the "Workspace" tab in `services/web`, or `http://localhost:5173/#workspace`.

**Full-screen office:** Workspace fills the browser viewport with the desks. Only the
Jobs/Workspace navigation, a compact video selector and connection indicator overlay
the canvas. There are no history, progress, costs, event-log or gate panels. Portrait
screens stack the rooms vertically. Choose a video to focus its desks or choose all
videos to see the aggregate office. Run detail is still polled internally to preserve
the selected video's accurate state, including concurrent activities and revisits.

The office follows the selection: active desks are highlighted, the latest finished
visit is labeled as such, desks without recorded activity are muted, and handoff animation
is limited to the latest handoff for that video. An unavailable selected-run detail is
shown as unknown rather than borrowing another video's desk state.

**Where the data comes from:** `GET /api/workspace/snapshot` in the downloader reads open workflows from Temporal and `localization.llm_calls` / `localization.runs` from Postgres. It writes nothing and returns no prompt, translation or transcript text. If a source is down the endpoint still answers 200 and says which one (`sources`); desks then read "không rõ" rather than "rảnh". `GET /api/workspace/runs/:awemeId/:generation` backs the detail panel (404 for an unknown run, 503 when a source that might know it is down).

**Configuration:** `TEMPORAL_ADDRESS` (default `temporal:7233`) and `TEMPORAL_NAMESPACE` (default `default`) on the downloader. Only `src/workspace` and `src/temporal` may import the Temporal client; a test enforces it and that nothing there can start or signal a workflow.

**What animates:** activity starts/retries set desk state; completions, failures and
LLM call events can open a desk bubble. Handoffs draw a line between desks. Gate
decisions remain result events in the log; there is no gate section or gate bubble.
The event feed retains events internally for deduplication; no event log is rendered.

Agent desks are labeled Localizer Agent, Dubber Agent and Reviewer Agent.
Review activities and JEV model calls share the Reviewer Agent desk;
JEV is part of review, not a separate handoff destination.
Open Temporal workflows are restricted to `VideoLocalizationWorkflow` and the opt-in
`WorkspaceSimulationWorkflow`; Vbee workflows are excluded. Existing historical
Postgres rows remain read-only and are not migrated or deleted.

**Limits:** detail is per activity. An agent that runs inside one long activity shows as "working" for the whole of it; there is no "thinking" state. Events come from workflows that are still open plus `llm_calls` and `runs`, so a run that finished more than a few seconds ago appears only through its cost and gate decision. GPU time in the panel is the duration of `stt` and `tts` activities, including queue wait; it is an estimate, not an invoice.

## Local simulation with real Temporal

The opt-in overlay runs a synthetic demonstration workflow with mock activities named
like the standard VoiceStudio pipeline (`LOCALIZER_MODE=single`, `DUBBER_MODE=code`,
`REVIEWER_MODE=jev`). It exercises Workspace's real Temporal reader, polling, states,
bubbles and handoffs. It does **not** validate the production localization workflow's
branching or payload contracts. No provider, object store, database write or actual
publication is used. The simulator receives no production credentials.

From the repository root, with the normal stack already available:

```sh
# Stop real processing before opening the demonstration.
docker compose stop localization-starter worker-agents worker-media
docker compose -f docker-compose.yml -f docker-compose.workspace-simulation.yml up -d --no-deps nest web workspace-simulator
```

The `reup-flow-worker-agents` image must exist (it is reused without rebuilding).
Open <http://localhost:5173/#workspace>.
The overlay builds the web with relative API URLs; nginx proxies `/api/` and
`/internal/` to the downloader. Other devices can use `http://<host-LAN-IP>:5173/#workspace`
without calling their own localhost. The same API-key authentication applies.

The downloader reads the isolated namespace
`workspace-simulation-voicestudio`; the default namespace and its workflows are untouched.
Earlier mixed-pipeline demos in `workspace-simulation` are left paused without a worker;
the new namespace avoids replaying those histories against a changed activity sequence.
Three demo videos run concurrently and repeat automatically:

| Video suffix | Scenario |
| --- | --- |
| `0001` | Every activity succeeds, including mock publish |
| `0002` | TTS fails twice, then succeeds on attempt 3 |
| `0003` | Review fails without retry; remains visible for 35 seconds |

Each activity waits six seconds. History is bounded with continue-as-new after five
cycles. Restarting the container resumes the existing workflows. Because Workspace
inspects open workflows, the demonstration keeps them open between cycles.
The representative path includes initial synthesis, a Dubber correction, resynthesis,
audio assembly, captions, review before render, render and mock publish. It deliberately
exercises the Dubber branch; preparation and TTS batches run sequentially in the demo,
whereas production can run them concurrently. JEV belongs inside review, with no separate
`review_jev` activity. There are no Slot Shortener, slot-planning or Vbee STT steps.

Postgres remains the existing read-only source: older real rows are retained, but the
video selector shows active Temporal runs. Demo cost is zero, and no demo gate decisions
or LLM calls are seeded.
The displayed STT/TTS duration is simulated waiting time.

Stop the demo and restore observation of the default namespace:

```sh
docker compose -f docker-compose.yml -f docker-compose.workspace-simulation.yml stop workspace-simulator
docker compose up -d --no-deps nest
```

This leaves real workers stopped. Resume actual processing only when desired:

```sh
docker compose up -d localization-starter worker-agents worker-media
```

While testing, avoid an untargeted `docker compose up -d`, which starts real workers
and restores the downloader's default namespace. Stopping the mock worker pauses its
work; the isolated Temporal executions remain available to resume later.
