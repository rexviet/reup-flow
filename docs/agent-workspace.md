# Agent Workspace

A page in the web app that shows which localization agent is working on which video, from real data.

**Open it:** the "Workspace" tab in `services/web`, or `http://localhost:5173/#workspace`.

**Where the data comes from:** `GET /api/workspace/snapshot` in the downloader reads open workflows from Temporal and `localization.llm_calls` / `localization.runs` from Postgres. It writes nothing and returns no prompt, translation or transcript text. If a source is down the endpoint still answers 200 and says which one (`sources`); desks then read "không rõ" rather than "rảnh". `GET /api/workspace/runs/:awemeId/:generation` backs the detail panel (404 for an unknown run, 503 when a source that might know it is down).

**Configuration:** `TEMPORAL_ADDRESS` (default `temporal:7233`) and `TEMPORAL_NAMESPACE` (default `default`) on the downloader. Only `src/workspace` and `src/temporal` may import the Temporal client; a test enforces it and that nothing there can start or signal a workflow.

**What animates (and nothing else):** `activity_started`/`activity_retrying` set a desk's state, `activity_completed`, `activity_failed`, `llm_call_recorded` and `gate_decision` open a one-line bubble, `handoff` draws a line between two desks. The event log keeps the latest 50.

**Limits:** detail is per activity. An agent that runs inside one long activity shows as "working" for the whole of it; there is no "thinking" state. Events come from workflows that are still open plus `llm_calls` and `runs`, so a run that finished more than a few seconds ago appears only through its cost and gate decision. GPU time in the panel is the duration of `stt` and `tts` activities, including queue wait; it is an estimate, not an invoice.
