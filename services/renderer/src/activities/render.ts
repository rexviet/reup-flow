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
import { createSpecValidator, specSha256, type EditSpec, type SpecValidator } from "../edit-spec";
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

// A render that has not finished by now is not going to: the spec's own limits (15 minutes
// at 120 fps at 4096 px) keep honest renders far under it.
export const DEFAULT_RENDER_DEADLINE_MS = 30 * 60 * 1000;
export const DEFAULT_HEARTBEAT_INTERVAL_MS = 5000;

export function renderKey(awemeId: string, specSha: string): string {
  if (!AWEME_ID.test(awemeId)) throw new Error("aweme_id must be 1-32 ASCII digits");
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
  return key.startsWith(`work/${awemeId}/`) && !key.split("/").some((part) => part.startsWith(".."));
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

export function createRenderActivity(deps: RenderDeps) {
  const validate = deps.validate ?? createSpecValidator();
  const heartbeat =
    deps.heartbeat ??
    ((progress: number) => {
      try {
        Context.current().heartbeat(progress);
      } catch {
        // Not inside an activity (a local render): nothing to tell.
      }
    });
  const cancellation = deps.cancellation ?? currentCancellation;
  const inFlight = new Map<string, Promise<RenderResult>>();

  async function renderOnce(spec: EditSpec, sha: string, outputKey: string): Promise<RenderResult> {
    const span = trace.getActiveSpan();
    const timeout = AbortSignal.timeout(deps.deadlineMs ?? DEFAULT_RENDER_DEADLINE_MS);
    const cancelled = cancellation();
    const signal = cancelled ? AbortSignal.any([cancelled, timeout]) : timeout;
    let progress = 0;
    // Alive while it downloads, renders and uploads, not only while Remotion reports.
    const beat = setInterval(
      () => heartbeat(progress),
      deps.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS,
    );
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
      signal.throwIfAborted();
      await deps.store.download(spec.audio_key, audio);
      signal.throwIfAborted();
      if (sha256File(audio) !== spec.audio_sha256) {
        refuse("the audio is not the track this spec was made for", "AudioHashMismatch");
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
            progress = p;
            heartbeat(p);
          },
          signal,
        });
      } catch (err) {
        if (timeout.aborted && !cancelled?.aborted) {
          refuse(`the render did not finish within its deadline`, "RenderDeadline");
        }
        throw err;
      }
      signal.throwIfAborted();
      await deps.store.uploadFile(outputKey, output, "video/mp4");
      return { render_key: outputKey, spec_sha256: sha };
    } finally {
      clearInterval(beat);
      await server?.close();
      if (dir) fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  return async function render(input: RenderInput): Promise<RenderResult> {
    if (!AWEME_ID.test(input.aweme_id)) refuse("aweme_id must be 1-32 digits", "InvalidInput");
    if (!underVideo(input.spec_key, input.aweme_id)) {
      refuse("spec_key is not under this video's work/ folder", "InvalidInput");
    }
    const spec = parseSpec(await deps.store.getBytes(input.spec_key), validate);
    for (const key of [spec.source_key, spec.audio_key]) {
      if (!underVideo(key, input.aweme_id)) {
        refuse(`${key} is not under this video's work/ folder`, "InvalidEditSpec");
      }
    }
    // Lines were measured with one font and would be drawn with another.
    checkFont(spec, deps.fontPath);

    const sha = specSha256(spec);
    const outputKey = renderKey(input.aweme_id, sha);
    trace.getActiveSpan()?.setAttribute("render.spec_sha256", sha);

    // The same spec asked for again while it is being rendered here waits for that render.
    // (Another replica cannot be seen from here: two workers can still render one spec at
    // once. That costs CPU and nothing else: the key is the hash, and an object is
    // written whole.)
    const running = inFlight.get(sha);
    if (running) return running;
    const mine = renderOnce(spec, sha, outputKey);
    inFlight.set(sha, mine);
    try {
      return await mine;
    } finally {
      inFlight.delete(sha);
    }
  };
}
