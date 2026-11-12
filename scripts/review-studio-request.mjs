// node scripts/review-studio-request.mjs <aweme> <generation>
//
// Opens one review project in Studio on behalf of the review worker (a dashboard "Mở
// Studio" request). A Studio already open for another project is backed up and stopped by
// `replaceStudio`; the launcher is started detached, so Studio outlives this helper and the
// worker, exactly like one opened by the auto-open watcher.
import { spawn } from 'node:child_process';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ROOT, validateIdentity } from './review-studio.mjs';
import { replaceStudio, studio } from './review-auto-open.mjs';

const pause = ms => new Promise(done => setTimeout(done, ms));

/** Replace the running Studio if it belongs to another project, then launch this one. */
export async function openProject(aweme, generation, { current, replace, launch }) {
  validateIdentity(aweme, generation);
  const active = await current();
  const same = active?.aweme === aweme && active?.generation === generation;
  if (active && active.pagesOpened !== true)
    throw new Error('Studio is still starting; try again in a moment');
  if (active && !same) await replace(active);
  await launch(aweme, generation, same);
}

/** Start the launcher detached and wait until its pages are open (or it exits early). */
export async function launchDetached(aweme, generation, reopen, deps = {}) {
  const { current = studio, spawnLauncher = spawnStudio, wait = pause } = deps;
  const child = spawnLauncher(aweme, generation);
  let exited = null, failure;
  child.once('exit', code => { exited = code ?? 1; });
  child.once('error', error => { failure = error; });
  for (let n = 0; n < 360; n++) {
    if (failure) throw new Error('Could not start the Studio launcher');
    if (reopen && exited !== null) {
      // The Studio was already running: the launcher only opens the pages and exits.
      if (exited === 0) return;
      throw new Error(`Studio launcher exited with ${exited}`);
    }
    if (!reopen) {
      if (exited !== null) throw new Error(`Studio launcher exited with ${exited}`);
      const active = await current();
      if (active?.aweme === aweme && active.generation === generation &&
          active.pagesOpened === true) return;
    }
    await wait(250);
  }
  throw new Error('Studio launch not confirmed');
}

function spawnStudio(aweme, generation) {
  const child = spawn(process.execPath, [join(ROOT, 'scripts/review-studio.mjs'),
    'open', aweme, generation], { cwd: ROOT, stdio: 'ignore', detached: true });
  child.unref();
  return child;
}

export async function run(args) {
  if (args.length !== 2) throw new Error('Usage: review-studio-request.mjs <aweme> <generation>');
  const [aweme, generation] = args;
  await openProject(aweme, generation, {
    current: studio,
    replace: replaceStudio,
    launch: (a, g, same) => launchDetached(a, g, same),
  });
  console.log(`Opened review ${aweme}/g${generation} in Studio`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  run(process.argv.slice(2)).catch(error => {
    // Launcher diagnostics can carry service details; keep only the helper's own message.
    console.error(error.message.split('\n')[0].slice(0, 300));
    process.exitCode = 1;
  });
