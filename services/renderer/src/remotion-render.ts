// The Remotion side: bundle the composition once, then render a spec to an mp4 file.
import * as path from "node:path";
import { bundle } from "@remotion/bundler";
import {
  makeCancelSignal,
  renderMedia,
  selectComposition,
} from "@remotion/renderer";
import { COMPOSITION_ID } from "./remotion/Root";
import type { LocalizedVideoProps } from "./remotion/LocalizedVideo";

// The composition's source, whether this file runs from src/ or from the compiled dist/.
const ENTRY_POINT = path.resolve(__dirname, "../src/remotion/index.ts");

/** Bundles the composition. Done once at startup: it takes seconds, a render must not. */
export async function bundleComposition(): Promise<string> {
  return bundle({ entryPoint: ENTRY_POINT });
}

export interface RenderVideoArgs extends LocalizedVideoProps {
  outputLocation: string;
  onProgress: (progress: number) => void;
  /** Aborting stops the render: Chromium and the compositor are shut down and the call rejects. */
  signal?: AbortSignal;
}

export type RenderVideo = (args: RenderVideoArgs) => Promise<void>;

// Remotion sizes itself from the machine it sees, which in a container is the whole host,
// not the container's memory limit. Both knobs below are set explicitly: browser tabs
// rendering frames at once, and the memory the video decoder may keep decoded frames in.
// (Measured in the image, see the phase notes; the defaults ran a 60 s clip out of a 4 GB
// container within 20 seconds.)
export const RENDER_TABS = 2;
export const VIDEO_CACHE_BYTES = 512 * 1024 * 1024;

// The image installs a pinned Chrome and names it here. Unset (a developer machine), Remotion
// uses the browser it downloads for its own version.
const browserExecutable = process.env.CHROME_EXECUTABLE || null;

export function remotionRenderer(serveUrl: string): RenderVideo {
  return async ({ spec, urls, outputLocation, onProgress, signal }) => {
    const inputProps = { spec, urls };
    const composition = await selectComposition({
      serveUrl,
      id: COMPOSITION_ID,
      inputProps,
      browserExecutable,
    });
    const { cancelSignal, cancel } = makeCancelSignal();
    const onAbort = () => cancel();
    if (signal?.aborted) cancel();
    signal?.addEventListener("abort", onAbort, { once: true });
    try {
      await renderMedia({
        composition,
        serveUrl,
        inputProps,
        codec: "h264",
        outputLocation,
        browserExecutable,
        concurrency: RENDER_TABS,
        offthreadVideoCacheSizeInBytes: VIDEO_CACHE_BYTES,
        cancelSignal,
        onProgress: ({ progress }) => onProgress(progress),
      });
    } finally {
      signal?.removeEventListener("abort", onAbort);
    }
  };
}
