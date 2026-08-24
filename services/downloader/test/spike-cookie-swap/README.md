# SPIKE 2 (GATE) -- Node writes Redis, parser uses the new cookie, no restart

Answers one question, from the Node side, with a real running parser: if
NestJS writes `douyin:cookie:current` to Redis, does the parser's *very next
request* use it, with no restart? This contract was implemented in the fork
(`crawlers/douyin/web/cookie_provider.py`) but had never been exercised from
Node. `plans/260824-2152-douyin-downloader/phase-00-skeleton-spike.md` sec 0.5
makes this a gate: fail here and phase 1 does not start.

## What actually runs, and what is faked

The harness changes exactly two things about the real parser container:
**where its DNS-level network destination points** (`extra_hosts` maps
`www.douyin.com` / `www.iesdouyin.com` to a fake TLS endpoint we control) and
**what its trust store contains** (a throwaway CA appended to certifi's
bundle at container start, via an `entrypoint` override, so the parser's
httpx client trusts our fake endpoint's certificate).

Everything else is the real, unmodified parser: `resolve_cookie_async()`,
the per-event-loop `redis.asyncio` client cache in `cookie_provider.py`, the
`get_douyin_headers()` call at the top of `fetch_one_video`, and the httpx
request that carries the resulting `Cookie` header, all execute exactly as
they do in production. No file inside `src/Douyin_TikTok_Download_API/` is
read, mounted over, or edited -- confirmed at the end of every run via
`git -C src/Douyin_TikTok_Download_API status --porcelain`.

The "echo" server (`echo/echo_server.py`) plays the role of Douyin: it
terminates TLS with a cert for `www.douyin.com`, records the `Cookie` header
of every request it receives to `data/captured.log` (one JSON line per
request), and returns a deliberately-fake, non-Douyin-shaped JSON body. The
parser's generic `except Exception -> 400` handler (F4 in the solution
design) then returns its usual static error envelope. **That 400 is
expected and correct** -- the only thing under test is what arrived in the
`Cookie` header, never the response body.

The echo server can also hold a request's connection open before responding:
the jest spec suffixes an `aweme_id` with `_hold<ms>` (the parser forwards
`aweme_id` verbatim as a query param on its outbound request -- see
`crawlers/douyin/web/web_crawler.py`'s `PostDetail` + `urlencode`), and the
echo server records the request immediately, then sleeps `<ms>` before
responding. This is what makes step 3's "still in flight during the swap"
claim (below) provable rather than assumed.

## What this does NOT prove

- Nothing about real Douyin: no real `a_bogus` validity, no real cookie
  format, no real anti-bot behavior. The echo server accepts anything.
- Nothing about the NestJS side's own Redis-write code (that does not exist
  yet in `services/downloader/src`); this harness writes with a bare
  `ioredis` client, matching only the wire contract (the same `SET`/`DEL` on
  `douyin:cookie:current`) the real service will eventually use.
- Nothing about sustained/production load, or about the reaper/lease logic
  described elsewhere in the solution design -- this is a point-in-time
  spike with 5 concurrent requests, not a load test.
- Nothing about `network_mode: host`, which the FORK's own
  `docker-compose.yml` (`src/Douyin_TikTok_Download_API/docker-compose.yml`)
  uses and which is incompatible with the static-IP bridge network
  `extra_hosts` needs here; this harness's compose file
  (`docker-compose.spike.yml`) is deliberately separate and is not a
  template for the real `docker-compose.yml`. (The repo ROOT's own
  `docker-compose.yml` does not use `network_mode: host` and is unrelated to
  this constraint.)

## Layout

```
setup-certs.sh              generates a throwaway CA + leaf cert at test time (never committed)
echo/echo_server.py         fake TLS "Douyin" endpoint; records Cookie headers, supports holding requests open
docker-compose.spike.yml    redis + echo + parser (built from the real fork), own bridge net + project name
helpers.ts                  ioredis/fetch/docker-inspect/proc-scan/captured-log helpers for the jest spec
cookie-swap.spike-test.ts   the 5-step spike scenario + anti-restart checks (see naming note below)
jest.spike.config.js        isolated jest config so `pnpm test` never picks this file up
tsconfig.spike.json         standalone tsconfig for the isolated jest config
run.sh                      up -> wait healthy -> run jest -> tear down (always), single entrypoint
mutations/                  the three required negative-control scripts (see below)
```

`cookie-swap.spike-test.ts` is named `*.spike-test.ts`, not `*.spec.ts` /
`*.e2e-spec.ts`, so it is invisible to the service's own `jest.config.js`
(`testRegex: '.*\.(spec|e2e-spec)\.ts$'`) and therefore to a plain
`pnpm -C services/downloader test`. It requires a live docker compose stack
and must only run through `run.sh`.

## Running it

```bash
pnpm -C services/downloader run test:spike-cookie-swap
# or directly:
bash services/downloader/test/spike-cookie-swap/run.sh
```

Brings the stack up on its own docker compose project (`spike-cookie-swap`,
host ports 16379/18080, no collision with the root stack's 5432/6379/3000),
waits for parser, redis, AND the echo TLS endpoint to be reachable (the
latter via a `docker compose exec` probe from inside the network, since echo
is deliberately not published to the host), runs the jest scenario, prints
parser logs on failure, and always tears the stack down (containers,
network, generated certs, capture log) via a shell `trap`, whether the tests
pass or fail.

## The scenario (`cookie-swap.spike-test.ts`)

1. Write sentinel A to `douyin:cookie:current`; call the parser; assert the
   echo server captured A.
2. Write sentinel B, no restart; call again; assert B.
3. (Mutation 3 hook, no-op by default -- see below.)
4. **5 concurrent requests, provably still in flight when the key is
   swapped mid-batch.** req0/req1 are dispatched and held open by the echo
   server; the harness waits for captured.log to show BOTH have actually
   arrived at echo (proof their Cookie header was already resolved from
   sentinel A -- cookie resolution happens inside the parser strictly
   before its outbound request) before recording `swapTime` and setting the
   key to B; req2-4 are then dispatched. Assertions:
   - req0/req1: `start < swapTime < end` -- genuinely still open (in
     flight) when the swap landed, not merely "issued earlier in program
     order".
   - Across the whole 5-request batch: `max(start) < min(end)` -- real
     temporal overlap, not an assumption about scheduling.
   - req0/req1 carried A (proven, per the causal argument above); req2-4
     carried B (deterministic: dispatched only after the awaited `SET`
     resolved).
   - A request after the batch settles also sees B.
5. Delete the key; assert the parser does not crash and falls back to
   `DOUYIN_COOKIE_FALLBACK` (a third, distinct sentinel, so fallback is
   distinguishable from stale cache).
6. **Anti-restart, worker identity (primary evidence).** The container's
   process tree, because `start.py` runs `uvicorn.run(..., reload=True)`, is:
   ```
   1   /bin/sh ./start.sh                              <- container's PID 1
   9   python3 start.py                                <- uvicorn --reload supervisor
   21  python3 ... spawn_main --multiprocessing-fork    <- ACTUALLY serves requests
   ```
   `docker inspect`'s `.State.Pid` / `.State.StartedAt` only describe PID 1
   and do NOT change if the reload supervisor respawns the worker -- an
   adversarial review killed the worker live and found those fields
   byte-identical before/after while the app was fully dead in between.
   `helpers.ts:inspectWorker` closes that gap: it resolves the worker
   inside the container by requiring an EXACT match on the last argv
   element of `/proc/<pid>/cmdline` (`--multiprocessing-fork`), then reads
   `/proc/<pid>/stat` field 22 (starttime in jiffies since boot -- monotonic,
   immune to PID reuse). Compared before step 1 and after step 5(4); equal,
   or the gate fails.
7. **Anti-restart, container identity (secondary signal).** `docker
   inspect`'s `StartedAt` + main `Pid` for the parser container, captured
   before step 1 and compared at the end. Kept as a second, independent
   signal (it catches the container itself being recreated) -- per the
   fix, it is no longer the ONLY evidence.

### The worker-identity self-matching pitfall (worth documenting)

An early version of `inspectWorker` matched the worker by substring-searching
`/proc/<pid>/cmdline` for `"multiprocessing-fork"`. That is wrong: the
`docker exec <container> python3 -c "<script>"` invocation used to *do the
matching* has that same string embedded in its own source, so it matched
itself -- and since every invocation spawns a brand-new (higher) PID, "pick
the highest matching PID" always returned the just-spawned lookup process,
making every single call look like a fresh restart even with zero mutation
applied. The fix requires an EXACT match on the LAST argv element (which for
the real `spawn_main` worker is the standalone flag `--multiprocessing-fork`,
never true for a `-c` invocation whose last argv element is the full script
text) -- see the comment above `FIND_WORKER_PY` / `KILL_WORKER_PY` in
`helpers.ts`.

## Required mutations (`mutations/`)

All three scripts drive the real `run.sh`, print the exact config change
applied, assert the RED run actually fails (`if ./run.sh; then ...exit 1;
fi` -- a mutation that silently passes is reported as a vacuous guard, never
as a pass), then restore and run again asserting GREEN:

- `01-wrong-key.sh` -- the jest test writes to `douyin:cookie:wrong`
  (env `SPIKE_COOKIE_KEY`) instead of the real key. Steps 1-3 must go RED
  (parser never reads that key, falls back to `DOUYIN_COOKIE_FALLBACK`)
  while steps 4-5(6-7) are unaffected. Proves the assertion is tied to the
  real key, not to something incidental.
- `02-unreachable-redis.sh` -- the parser's own `REDIS_URL` (env
  `SPIKE_REDIS_URL`, substituted into the compose file) is pointed at an
  unassigned IP in the spike subnet. Steps 1-3 must go RED, but step 4
  (fallback / no crash) and both anti-restart checks must still hold -- and
  the parser's own log line
  (`cookie_provider: Redis unavailable (TimeoutError), using fallback`)
  confirms the mutation actually reached the code path under test, not just
  that some other unrelated thing broke.
- `03-restart-parser.sh` -- reproduces, on demand, the exact false green an
  adversarial review found live: env `SPIKE_INJECT_WORKER_RESTART=1` makes a
  dedicated `it()` block in `cookie-swap.spike-test.ts` (a no-op otherwise,
  positioned between steps 2 and 3) `docker exec` into the running parser
  container and SIGKILL the serving `multiprocessing-fork` worker, then wait
  for uvicorn's `--reload` supervisor to respawn a fresh one before letting
  the scenario continue. The worker-identity anti-restart check must go RED;
  the container-identity check must stay GREEN (the container itself is
  never touched) -- which is exactly why the container-level check ALONE
  was insufficient (this is the fix for the FG this mutation targets).

Mutation required by the DoD ("with the key deleted, parser must not
crash") is not a separate script: it is step 5 of the main scenario above,
run and shown green on every invocation, including inside all three
mutation scripts' "restore" pass.
