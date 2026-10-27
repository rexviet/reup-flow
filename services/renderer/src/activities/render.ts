// Step 7: render one mp4 for one spec.
//
// The output key carries the hash of the spec, `work/{aweme_id}/render-{spec_sha256}.mp4`.
// If that object exists the spec has already been rendered and its key is returned: no
// sidecar file, and no half-written state either, because an object appears whole or not
// at all. Everything else runs against a throw-away directory that is always removed.
//
// A render is also bounded in time (a deadline), stoppable (the activity's cancellation
// reaches Remotion), visibly alive (a heartbeat while it downloads, renders and uploads)
// and shared: a second request for a spec that is being rendered in this process waits for
// that render instead of starting its own.
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { ApplicationFailure, Context } from "@temporalio/activity";
import { trace } from "@opentelemetry/api";
import { startAssetServer } from "../asset-server";
import {
  createSpecValidator,
  specSha256,
  type EditSpec,
  type SpecValidator,
} from "../edit-spec";
import { checkFont } from "../font";
import type { ObjectStore } from "../object-store";
import type { RenderVideo } from "../remotion-render";

export interface RenderInput {
  aweme_id: string;
  spec_key: string;
}

export interface RenderResult {
  render_key: string;
  spec_sha256: string;
}

const AWEME_ID = /^[0-9]{1,32}$/;
const SHA256 = /^[0-9a-f]{64}$/;

// A render that has not finished by now is not going to. The deadline grows with the video:
// a render was measured at about 3 seconds of wall clock per second of video, so the
// allowance is twice that, on top of a fixed time for starting Chromium and moving files.
export const RENDER_DEADLINE_BASE_MS = 5 * 60 * 1000;
export const RENDER_DEADLINE_PER_VIDEO_MS = 6;

export function renderDeadlineMs(durationMs: number): number {
  return RENDER_DEADLINE_BASE_MS + RENDER_DEADLINE_PER_VIDEO_MS * durationMs;
}
export const DEFAULT_HEARTBEAT_INTERVAL_MS = 5000;

export function renderKey(awemeId: string, specSha: string): string {
  if (!AWEME_ID.test(awemeId))
    throw new Error("aweme_id must be 1-32 ASCII digits");
  if (!SHA256.test(specSha)) throw new Error("expected a lowercase hex sha256");
  return `work/${awemeId}/render-${specSha}.mp4`;
}

/** How many times `renderMedia` actually ran, per spec hash. A spec that is already
 * rendered must not raise its count. */
export class RenderInvocations {
  private readonly counts = new Map<string, number>();

  record(specSha: string): number {
    const next = (this.counts.get(specSha) ?? 0) + 1;
    this.counts.set(specSha, next);
    return next;
  }

  count(specSha: string): number {
    return this.counts.get(specSha) ?? 0;
  }
}

export interface RenderDeps {
  store: ObjectStore;
  renderVideo: RenderVideo;
  fontPath: string;
  invocations: RenderInvocations;
  validate?: SpecValidator;
  heartbeat?: (progress: number) => void;
  heartbeatIntervalMs?: number;
  /** Overrides the deadline that is otherwise worked out from the video's length. */
  deadlineMs?: number;
  /** The signal that is aborted when the activity is cancelled. */
  cancellation?: () => AbortSignal | undefined;
  tmpRoot?: string;
}

function refuse(message: string, type: string): never {
  throw ApplicationFailure.nonRetryable(message, type);
}

function sha256File(file: string): string {
  return createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function underVideo(key: string, awemeId: string): boolean {
  return (
    key.startsWith(`work/${awemeId}/`) &&
    !key.split("/").some((part) => part.startsWith(".."))
  );
}

function parseSpec(raw: Buffer, validate: SpecValidator): EditSpec {
  let json: unknown;
  try {
    json = JSON.parse(raw.toString("utf8"));
  } catch {
    // The same bytes fail the same way.
    refuse("the edit spec is not valid JSON", "InvalidEditSpec");
  }
  return validate(json);
}

function currentCancellation(): AbortSignal | undefined {
  try {
    return Context.current().cancellationSignal;
  } catch {
    return undefined; // not inside an activity (a local render)
  }
}

type Heartbeat = (progress: number) => void;

/** A heartbeat that goes to the activity that is running now, whichever async context
 * later calls it. */
function heartbeatOfThisActivity(): Heartbeat {
  try {
    const context = Context.current();
    return (progress) => context.heartbeat(progress);
  } catch {
    return () => undefined; // not inside an activity (a local render)
  }
}

/** One render in progress, shared by every caller that asked for its spec. It belongs to
 * none of them: it is stopped when the last one has gone, not when the first one does. */
interface SharedRender {
  promise: Promise<RenderResult>;
  callers: number;
  stop: AbortController;
  listeners: Set<(progress: number) => void>;
  progress: number;
}

/** `promise`, or the caller's own reason if it is cancelled first. */
function untilCancelled<T>(
  promise: Promise<T>,
  own: AbortSignal | undefined,
): Promise<T> {
  if (!own) return promise;
  if (own.aborted) return Promise.reject(own.reason);
  let onAbort!: () => void;
  const cancelled = new Promise<never>((_resolve, reject) => {
    onAbort = () => reject(own.reason);
    own.addEventListener("abort", onAbort, { once: true });
  });
  return Promise.race([promise, cancelled]).finally(() =>
    own.removeEventListener("abort", onAbort),
  );
}

export function createRenderActivity(deps: RenderDeps) {
  const validate = deps.validate ?? createSpecValidator();
  const cancellation = deps.cancellation ?? currentCancellation;
  const inFlight = new Map<string, SharedRender>();

  async function renderOnce(
    spec: EditSpec,
    sha: string,
    outputKey: string,
    shared: SharedRender,
  ): Promise<RenderResult> {
    const span = trace.getActiveSpan();
    const timeout = AbortSignal.timeout(
      deps.deadlineMs ?? renderDeadlineMs(spec.duration_ms),
    );
    const signal = AbortSignal.any([shared.stop.signal, timeout]);
    // Stopping because the time is up is a different thing from being stopped.
    const check = () => {
      if (!signal.aborted) return;
      if (timeout.aborted && !shared.stop.signal.aborted) {
        refuse(
          "the render did not finish within its deadline",
          "RenderDeadline",
        );
      }
      throw signal.reason;
    };
    let dir: string | undefined;
    let server: Awaited<ReturnType<typeof startAssetServer>> | undefined;
    try {
      if (await deps.store.exists(outputKey)) {
        span?.setAttribute("render.reused", true);
        return { render_key: outputKey, spec_sha256: sha };
      }
      dir = fs.mkdtempSync(path.join(deps.tmpRoot ?? os.tmpdir(), "render-"));
      const source = path.join(dir, "source.mp4");
      const audio = path.join(dir, "audio.wav");
      await deps.store.download(spec.source_key, source);
      check();
      await deps.store.download(spec.audio_key, audio);
      check();
      if (sha256File(audio) !== spec.audio_sha256) {
        refuse(
          "the audio is not the track this spec was made for",
          "AudioHashMismatch",
        );
      }
      fs.copyFileSync(deps.fontPath, path.join(dir, "font.ttf"));
      const output = path.join(dir, "render.mp4");

      server = await startAssetServer(dir);
      const calls = deps.invocations.record(sha);
      span?.setAttribute("render.media_invocations", calls);
      try {
        await deps.renderVideo({
          spec,
          urls: {
            source: server.url("source.mp4"),
            audio: server.url("audio.wav"),
            font: server.url("font.ttf"),
          },
          outputLocation: output,
          onProgress: (p) => {
            shared.progress = p;
            for (const listener of shared.listeners) listener(p);
          },
          signal,
        });
      } catch (err) {
        check();
        throw err;
      }
      check();
      await deps.store.uploadFile(outputKey, output, "video/mp4");
      return { render_key: outputKey, spec_sha256: sha };
    } finally {
      await server?.close();
      if (dir) fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  return async function render(input: RenderInput): Promise<RenderResult> {
    if (!AWEME_ID.test(input.aweme_id))
      refuse("aweme_id must be 1-32 digits", "InvalidInput");
    if (!underVideo(input.spec_key, input.aweme_id)) {
      refuse("spec_key is not under this video's work/ folder", "InvalidInput");
    }
    const spec = parseSpec(await deps.store.getBytes(input.spec_key), validate);
    for (const key of [spec.source_key, spec.audio_key]) {
      if (!underVideo(key, input.aweme_id)) {
        refuse(
          `${key} is not under this video's work/ folder`,
          "InvalidEditSpec",
        );
      }
    }
    // Lines were measured with one font and would be drawn with another.
    checkFont(spec, deps.fontPath);

    const sha = specSha256(spec);
    const outputKey = renderKey(input.aweme_id, sha);
    trace.getActiveSpan()?.setAttribute("render.spec_sha256", sha);

    // The same spec asked for again while it is being rendered here joins that render.
    // (Another replica cannot be seen from here: two workers can still render one spec at
    // once. That costs CPU and nothing else: the key is the hash, and an object is
    // written whole.)
    let shared = inFlight.get(sha);
    if (!shared) {
      const started: SharedRender = {
        promise: undefined as unknown as Promise<RenderResult>,
        callers: 0,
        stop: new AbortController(),
        listeners: new Set(),
        progress: 0,
      };
      started.promise = renderOnce(spec, sha, outputKey, started).finally(
        () => {
          if (inFlight.get(sha) === started) inFlight.delete(sha);
        },
      );
      // Nobody may be left listening when it fails after its last caller went.
      started.promise.catch(() => undefined);
      inFlight.set(sha, started);
      shared = started;
    }
    const joined = shared;

    // Each caller is alive, and can be stopped, on its own account.
    const heartbeat = deps.heartbeat ?? heartbeatOfThisActivity();
    const listener = (progress: number) => heartbeat(progress);
    joined.listeners.add(listener);
    joined.callers += 1;
    const beat = setInterval(
      () => heartbeat(joined.progress),
      deps.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS,
    );
    const own = cancellation();
    try {
      return await untilCancelled(joined.promise, own);
    } finally {
      clearInterval(beat);
      joined.listeners.delete(listener);
      joined.callers -= 1;
      if (joined.callers === 0) {
        // The last one has gone: nobody wants this render any more. A caller that arrives
        // from now on starts its own instead of joining one that is stopping.
        if (inFlight.get(sha) === joined) inFlight.delete(sha);
        joined.stop.abort(new Error("every caller of this render has gone"));
        // Let it clean up before this activity reports that it is over.
        await joined.promise.catch(() => undefined);
      }
    }
  };
}
