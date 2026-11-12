// The parts of the localization stack that run on this Mac, under pm2.
// `scripts/start-all.sh` starts and stops them; to work on one by hand:
//   pm2 logs reup-voice-worker      pm2 reload reup-voice-worker      pm2 stop reup-voice-worker
// REUP_NODE is the Node (22.13+) the scripts run with; start-all.sh sets it.
const interpreter = process.env.REUP_NODE || 'node';

module.exports = {
  apps: [
    {
      // Speech to text and text to speech (the `stt` and `tts` queues). It must be running
      // whenever the agents container has VOICE_QUEUES_ON_HOST=true, or speech tasks wait.
      name: 'reup-voice-worker',
      script: 'scripts/host-voice-worker.mjs',
      interpreter,
      cwd: __dirname,
      autorestart: true,
      // A start that dies within this time counts as a crash; after max_restarts in a row
      // pm2 stops retrying and shows `errored` instead of looping forever.
      min_uptime: '20s',
      max_restarts: 10,
      restart_delay: 5000,
      // The worker drains running activities for up to 90 s on SIGTERM; give it longer.
      kill_timeout: 100000,
      time: true,
    },
    {
      // Opens HyperFrames Studio for each project that becomes ready for review.
      name: 'reup-review-auto-open',
      script: 'scripts/review-auto-open.mjs',
      interpreter,
      cwd: __dirname,
      autorestart: true,
      min_uptime: '10s',
      max_restarts: 20,
      restart_delay: 3000,
      kill_timeout: 20000,
      time: true,
    },
    {
      // Final renders of approved review projects, review scans, Studio open requests from
      // the dashboard and the heartbeat. A render can run for minutes: on SIGTERM the worker
      // waits up to 60 s, hands the row back to the queue and kills the renderer, so pm2
      // must not cut in before that (pm2's default is about 1.6 s).
      name: 'reup-review-worker',
      script: 'scripts/review-worker.mjs',
      interpreter,
      cwd: __dirname,
      autorestart: true,
      min_uptime: '20s',
      max_restarts: 10,
      restart_delay: 5000,
      kill_timeout: 90000,
      // pm2 would otherwise kill the whole process tree at once, which ends the renderer
      // before the worker can wait for it and return the row to the queue.
      treekill: false,
      time: true,
    },
  ],
};
