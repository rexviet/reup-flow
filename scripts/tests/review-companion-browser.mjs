// Explicit synthetic-fixture browser check. Never run against an operator's project.
import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

if (process.env.REVIEW_BROWSER_FIXTURE !== '1')
  throw new Error(
    'Start the synthetic review_companion fixture and set REVIEW_BROWSER_FIXTURE=1',
  );
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const exec = promisify(execFile);
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const profile = await mkdtemp(join(tmpdir(), 'review-browser-check-'));
let browser, socket;
try {
  // Only the private pipe carries a grant; no argv, log or report contains it.
  const { stdout } = await exec(
    'uv',
    [
      'run',
      'python',
      '-c',
      `from pathlib import Path; from localization.review_bridge.auth import Auth; print(Auth(Path(${JSON.stringify(join(root, 'data/phase04-ui/runtime'))})).issue('7001',1))`,
    ],
    { cwd: join(root, 'services/localization') },
  );
  const grant = stdout.trim();
  assert.match(grant, /^[A-Za-z0-9_-]{43}$/);
  browser = spawn(
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    [
      '--headless=new',
      '--remote-debugging-port=0',
      `--user-data-dir=${profile}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      '--autoplay-policy=no-user-gesture-required',
      'about:blank',
    ],
    { stdio: ['ignore', 'ignore', 'pipe'] },
  );
  const endpoint = await new Promise((accept, reject) => {
    let text = '';
    const timer = setTimeout(
      () => reject(new Error('Browser startup timed out')),
      15000,
    );
    browser.stderr.on('data', (data) => {
      text += data;
      const match = text.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) {
        clearTimeout(timer);
        accept(match[1]);
      }
    });
    browser.once('error', reject);
  });
  socket = new WebSocket(endpoint);
  await new Promise((accept, reject) => {
    socket.onopen = accept;
    socket.onerror = reject;
  });
  let next = 1;
  const pending = new Map();
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (!pending.has(message.id)) return;
    const { accept, reject, timer } = pending.get(message.id);
    clearTimeout(timer);
    pending.delete(message.id);
    message.error
      ? reject(new Error('Browser command failed'))
      : accept(message.result);
  };
  function send(method, params = {}, sessionId) {
    return new Promise((accept, reject) => {
      const id = next++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} timed out`));
      }, 15000);
      pending.set(id, { accept, reject, timer });
      socket.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }
  const { targetId } = await send('Target.createTarget', {
    url: 'about:blank',
  });
  const { sessionId } = await send('Target.attachToTarget', {
    targetId,
    flatten: true,
  });
  const page = (method, params) => send(method, params, sessionId);
  await page('Page.enable');
  async function evaluate(expression) {
    const result = await page('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      const description = result.exceptionDetails.exception?.description?.split('\n')[0] || result.exceptionDetails.text;
      throw new Error('Browser expression failed: ' + description);
    }
    return result.result.value;
  }
  async function until(expression) {
    for (let count = 0; count < 150; count++) {
      if (await evaluate(expression)) return;
      await sleep(200);
    }
    throw new Error('Browser behavior did not reach its expected state');
  }
  await page('Page.navigate', { url: `http://127.0.0.1:3310/#${grant}` });
  await until(`document.querySelectorAll('.cue').length === 2`);
  assert.equal(await evaluate('location.hash'), '');
  await evaluate(
    `document.querySelector('.cue .actions button:nth-child(2)').click()`,
  );
  await until(`localStorage.length > 0`);
  await page('Page.reload');
  await until(
    `document.querySelector('#notice')?.textContent.includes('Đã cập nhật giọng')`,
  );
  assert.equal(
    await evaluate(
      `document.querySelector('.cue h3').textContent.includes('Đã đồng bộ')`,
    ),
    true,
  );
  const playback = await evaluate(`(async()=>{
    const frame=document.querySelector('#preview').contentDocument;
    const video=frame.querySelector('video'),mix=frame.querySelector('audio');
    await video.play();await new Promise(done=>setTimeout(done,400));
    const result={muted:video.muted,playing:!mix.paused,time:mix.currentTime};
    video.pause();return result;
  })()`);
  assert.equal(playback.muted, true);
  assert.equal(playback.playing, true);
  assert.ok(playback.time > 0);
  const literal = '</script><img src=x onerror="document.body.remove()">';
  await evaluate(`(()=>{
    const fields=document.querySelectorAll('.cue textarea');
    fields[1].value='Bản nhập chưa lưu';fields[1].dispatchEvent(new Event('input',{bubbles:true}));
    fields[0].value=${JSON.stringify(literal)};fields[0].dispatchEvent(new Event('input',{bubbles:true}));
    document.querySelector('.cue .actions button').click();
  })()`);
  await until(
    `document.querySelector('#notice')?.textContent.includes('Đã lưu phụ đề')`,
  );
  assert.equal(
    await evaluate(`document.querySelectorAll('.cue textarea')[1].value`),
    'Bản nhập chưa lưu',
  );
  assert.equal(await evaluate(`document.querySelectorAll('img').length`), 0);
  assert.equal(
    await evaluate(
      `fetch('/api/srt').then(r=>r.text()).then(t=>t.includes(${JSON.stringify(literal)}))`,
    ),
    true,
  );
  assert.equal(
    await evaluate(
      `document.querySelector('.cue h3').textContent.includes('chưa đồng bộ')`,
    ),
    true,
  );
  assert.equal(await evaluate('localStorage.length'), 0);
  await page('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 20, y: 20 });
  console.log(
    'Browser passed: grant removal, revoice/reload recovery, audible mixed playback, literal caption/SRT, retained unsaved row and no automatic revoice.',
  );
  await send('Browser.close');
} finally {
  socket?.close();
  if (browser && browser.exitCode === null) browser.kill('SIGTERM');
  await sleep(500);
  await rm(profile, { recursive: true, force: true });
}
