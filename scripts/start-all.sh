#!/usr/bin/env bash
# Start (or stop) the whole localization stack: Docker services first, then the host
# processes, which run under pm2.
#
#   scripts/start-all.sh [start] [--no-build] [--no-host]
#   scripts/start-all.sh stop [--no-host]
#   scripts/start-all.sh reload        reload the host processes (zero-downtime restart)
#   scripts/start-all.sh status
#
# start   Docker: infrastructure, localization-starter, worker-agents, worker-media and
#         review-bridge (the `review` profile). Host, under pm2 (ecosystem.config.cjs):
#           reup-voice-worker      the stt and tts queues, next to VoiceStudio
#           reup-ingest-worker     the ingest queue: Demucs on the GPU (run
#                                  scripts/setup-host-demucs.sh once first)
#           reup-review-auto-open  opens HyperFrames Studio for each ready project
#           reup-review-worker     final renders of approved projects, review scans,
#                                  dashboard "open Studio" requests and the heartbeat
#         The watcher used to be a macOS LaunchAgent; if that is still loaded it is stopped
#         first, so two watchers never open the same project.
# stop    Stops the host processes and Studio, then the Docker services this script started.
#         Volumes and queued state are kept. Speech tasks wait in Temporal until the voice
#         worker is started again.
# reload  pm2 reload of the host processes, e.g. after changing worker code.
# status  The Docker services, the pm2 processes and whether VoiceStudio answers.
#
# --no-build  Reuse the images already built.
# --no-host   Docker only; leave the pm2 processes alone.
#
# Day to day: `pm2 logs reup-voice-worker`, `pm2 reload <name>`, `pm2 stop <name>`.
# Targeted `up` on purpose: an untargeted `docker compose up -d` also starts services
# that should stay off while testing (see README).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

ACTION=start
BUILD=--build
HOST=1
while [ $# -gt 0 ]; do
  case "$1" in
    start|stop|status|reload) ACTION="$1"; shift ;;
    --no-build) BUILD=""; shift ;;
    --no-host) HOST=0; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

# The localization services, in the order they depend on each other. Compose pulls in
# Postgres, Redis, Temporal and MinIO through `depends_on`.
SERVICES=(localization-starter worker-agents worker-media review-bridge)
DC=(docker compose --profile review)
HOST_APPS=(reup-voice-worker reup-ingest-worker reup-review-auto-open reup-review-worker)
VOICESTUDIO_URL=http://127.0.0.1:3900/.well-known/voicestudio-speech
LAUNCH_AGENT=com.reup-flow.review-auto-open

need_docker() {
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (open Docker Desktop)"
}

need_pm2() {
  command -v pm2 >/dev/null 2>&1 || die "pm2 is not installed (npm install -g pm2)"
}

# The Studio and the watcher need Node 22.13 or newer, and the shell's default node is not
# always that (nvm). Picks the first that is.
node_ok() { "$1" -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a===22&&b>=13)?0:1)' 2>/dev/null; }
find_node() {
  local candidate
  for candidate in "$(command -v node 2>/dev/null || true)" \
    $(ls -d "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -rV); do
    [ -n "$candidate" ] && [ -x "$candidate" ] && node_ok "$candidate" && { echo "$candidate"; return; }
  done
  return 1
}

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

wait_for() { # <description> <seconds> <command...>
  local what="$1" seconds="$2"; shift 2
  for _ in $(seq 1 "$seconds"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  die "$what did not become ready within ${seconds}s"
}

voicestudio_up() { curl -fsS -m 5 "$VOICESTUDIO_URL" >/dev/null 2>&1; }

pm2_field() { # <app> <jq-like path in pm2 jlist>: prints one field, empty if the app is unknown
  pm2 jlist 2>/dev/null | node -e '
    let s=""; process.stdin.on("data", d => s += d).on("end", () => {
      const i = s.indexOf("["); const apps = JSON.parse(s.slice(i));
      const a = apps.find(x => x.name === process.argv[1]); if (!a) return;
      const v = process.argv[2].split(".").reduce((o, k) => o?.[k], a); if (v != null) console.log(v);
    })' "$1" "$2"
}

host_start() {
  need_pm2
  NODE=$(find_node) || die "Node 22.13+ was not found (the Studio watcher needs it)"
  if ! voicestudio_up; then
    echo "VoiceStudio does not answer on $VOICESTUDIO_URL: starting it (MLX Whisper, patched runtime)"
    python3 scripts/launch-voicestudio.py || die "VoiceStudio did not start; see: scripts/launch-voicestudio.py --check"
  fi
  voicestudio_up || die "VoiceStudio does not answer on $VOICESTUDIO_URL"
  for port in 7233 9000; do
    wait_for "Docker port 127.0.0.1:$port" 60 port_open "$port"
  done

  # The watcher moves from the LaunchAgent to pm2.
  if [ "$(uname)" = Darwin ] && launchctl print "gui/$(id -u)/$LAUNCH_AGENT" >/dev/null 2>&1; then
    echo "stopping the $LAUNCH_AGENT LaunchAgent: pm2 runs the watcher now"
    python3 scripts/install-review-auto-open.py stop
  fi

  # Python's logging goes to stderr, so the line is in pm2's error log; look in both, and
  # count only a line written after this start (an older one proves nothing).
  logs=()
  for field in pm2_env.pm_out_log_path pm2_env.pm_err_log_path; do
    path=$(pm2_field reup-voice-worker "$field")
    [ -n "$path" ] && logs+=("$path")
  done
  ingest_logs=()
  for field in pm2_env.pm_out_log_path pm2_env.pm_err_log_path; do
    path=$(pm2_field reup-ingest-worker "$field")
    [ -n "$path" ] && ingest_logs+=("$path")
  done
  served() { cat "${logs[@]}" 2>/dev/null | grep -c "serving queues \['stt', 'tts'\]" || true; }
  served_ingest() { cat "${ingest_logs[@]}" 2>/dev/null | grep -c "serving queues \['ingest'\]" || true; }
  before=$(served)
  before_ingest=$(served_ingest)
  REUP_NODE="$NODE" pm2 startOrReload ecosystem.config.cjs --update-env >/dev/null
  if [ ${#logs[@]} -eq 0 ]; then
    for field in pm2_env.pm_out_log_path pm2_env.pm_err_log_path; do
      logs+=("$(pm2_field reup-voice-worker "$field")")
    done
  fi
  if [ ${#ingest_logs[@]} -eq 0 ]; then
    for field in pm2_env.pm_out_log_path pm2_env.pm_err_log_path; do
      ingest_logs+=("$(pm2_field reup-ingest-worker "$field")")
    done
  fi
  echo "waiting for the voice worker to serve the speech queues"
  voice_ready=0
  for _ in $(seq 1 90); do
    status=$(pm2_field reup-voice-worker pm2_env.status)
    [ "$status" = errored ] && die "reup-voice-worker failed to start; see: pm2 logs reup-voice-worker --lines 50"
    if [ "$(served)" -gt "$before" ]; then
      echo "reup-voice-worker is serving stt and tts"
      voice_ready=1
      break
    fi
    sleep 1
  done
  [ "$voice_ready" = 1 ] || die "reup-voice-worker did not report serving its queues; see: pm2 logs reup-voice-worker --lines 50"
  echo "waiting for the ingest worker to serve the ingest queue"
  for _ in $(seq 1 90); do
    status=$(pm2_field reup-ingest-worker pm2_env.status)
    [ "$status" = errored ] && die "reup-ingest-worker failed to start (has scripts/setup-host-demucs.sh been run?); see: pm2 logs reup-ingest-worker --lines 50"
    if [ "$(served_ingest)" -gt "$before_ingest" ]; then
      echo "reup-ingest-worker is serving ingest"
      return 0
    fi
    sleep 1
  done
  die "reup-ingest-worker did not report serving its queue; see: pm2 logs reup-ingest-worker --lines 50"
}

case "$ACTION" in
  start)
    say "checking Docker"
    need_docker
    [ -f .env ] || die ".env is missing; run scripts/bootstrap.sh first"

    say "starting Docker services"
    "${DC[@]}" up -d $BUILD "${SERVICES[@]}"

    say "waiting for review-bridge to be healthy"
    for _ in $(seq 1 30); do
      state=$(docker inspect --format '{{.State.Health.Status}}' \
        "$("${DC[@]}" ps -q review-bridge)" 2>/dev/null || echo starting)
      [ "$state" = healthy ] && break
      sleep 2
    done
    [ "${state:-}" = healthy ] || die "review-bridge is not healthy: ${state:-unknown}"

    if [ "$HOST" = 1 ]; then
      say "starting the host processes (pm2)"
      host_start
    fi
    say "ready"
    "${DC[@]}" ps "${SERVICES[@]}"
    if [ "$HOST" = 1 ]; then pm2 ls; fi
    ;;
  reload)
    need_pm2
    NODE=$(find_node) || die "Node 22.13+ was not found"
    REUP_NODE="$NODE" pm2 reload ecosystem.config.cjs --update-env
    ;;
  stop)
    if [ "$HOST" = 1 ] && command -v pm2 >/dev/null 2>&1; then
      say "stopping the host processes and Studio"
      for app in "${HOST_APPS[@]}"; do
        [ -n "$(pm2_field "$app" name)" ] && pm2 stop "$app" >/dev/null || true
      done
      NODE=$(find_node || true)
      [ -n "$NODE" ] && "$NODE" scripts/review-studio.mjs stop || true
    fi
    say "stopping Docker services"
    need_docker
    "${DC[@]}" stop "${SERVICES[@]}"
    ;;
  status)
    need_docker
    "${DC[@]}" ps "${SERVICES[@]}"
    say "host processes (pm2)"
    if command -v pm2 >/dev/null 2>&1; then
      for app in "${HOST_APPS[@]}"; do
        printf '%-24s %s\n' "$app" "$(pm2_field "$app" pm2_env.status || true)"
      done
    else
      echo "pm2 is not installed"
    fi
    voicestudio_up && echo "VoiceStudio: answering on 127.0.0.1:3900" \
      || echo "VoiceStudio: NOT answering on 127.0.0.1:3900"
    if [ "$(uname)" = Darwin ] && launchctl print "gui/$(id -u)/$LAUNCH_AGENT" >/dev/null 2>&1; then
      echo "warning: the $LAUNCH_AGENT LaunchAgent is still loaded and would run a second watcher"
    fi
    ;;
esac
