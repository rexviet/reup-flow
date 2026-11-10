# HyperFrames compatibility spike

Managed launcher follow-up, 2026-11-10: repository-owned HTTP boundary now guards
the unchanged pin; the full canary plays with a populated 03:09 timeline.
[Evidence and remaining gates](../plans/reports/ops-261110-1916-native-studio-full-video.md).
This does not claim the unwrapped vendor server passes its boundary checks.

This is a local, provider-free experiment under
`services/localization/spikes/hyperframes-review`. It does not change the production
pipeline. The selected package remains exactly `hyperframes@0.8.77`; provenance and
trusted inline-script hashes are recorded beside the fixture. A newer `0.9.26` was
probed separately and was not adopted.

Use Node 22.13 or newer and pnpm 11.23.0. The nested workspace owns its lockfile.
From the spike directory:

```sh
pnpm install --frozen-lockfile
sh prepare-media.sh /absolute/path/source.mp4 /absolute/path/mixed.wav
python3 review_adapter.py generate
pnpm test
pnpm exec hyperframes check project
pnpm run studio
```

Media preparation takes eight seconds beginning at source time 24 seconds. The video
is muted; the project has exactly one active mixed audio track. Local media, Studio
history and waveform caches are ignored by Git. The composition itself is ordinary
HTML/CSS with stable cue element IDs. Stop the foreground launcher with Ctrl+C; it
forwards termination to its child. Port 18440 is fixed, bound to loopback by the pinned
CLI, and an occupied port causes an error rather than another server. The launcher
maps `/review/project` inside the fixture bind mount to the host project using resolved
paths and argument arrays. It does not run inside the application container or mount
the Docker socket. Cross-session PID reuse is not implemented by this fixture.

Text and timing remain companion-owned. Regeneration captures only approved inline
visual properties from Studio-saved HTML by stable cue ID before writing new source.
The bridge fixture accepts revision-checked visual patches only, at
`http://127.0.0.1:18441/visual`; start it with `python3 review_adapter.py bridge`.
It has no provider or storage credentials and is not the production companion.
SRT export excludes blank review rows. Import requires unique unchanged timing anchors;
it refuses changed, missing or ambiguous mappings rather than trusting sequence numbers.
The native CLI SRT importer strips HTML-like text, so it is unsuitable as the canonical
literal-text round trip. Cue text and reasons stay JSON data and are escaped in HTML.

From `services/localization`, run the Python contracts and the isolated storage probe:

```sh
uv run pytest -q tests/spikes/test_hyperframes_review_contract.py
uv run python spikes/hyperframes-review/probe-storage.py
```

The storage probe needs Docker and the existing `reup-flow-minio-init:latest` helper
image. It creates a disposable MinIO container named `reup-hyperframes-cas-spike` on
loopback port 18443 with an in-memory bucket and generated credentials. It tests with a
review-only identity, then stops the container. It never uses the application's bucket
or credentials. A busy port or container name is refused. Do not interpret this isolated
backend test as proof of the production deployment's policy.

Both tested Studio versions accepted bad Host/Origin on the read-only project listing;
no permissive CORS response header was observed. The strict Studio boundary gate remains
unmet. Do not connect production credentials or review data until the boundary is resolved.
See the [execution evidence](../plans/reports/cook-261109-2018-hyperframes-phase-01.md)
for the tested behavior, screenshot comparison and remaining gates.
