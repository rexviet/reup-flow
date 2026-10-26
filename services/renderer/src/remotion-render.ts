// The Remotion side: bundle the composition once, then render a spec to an mp4 file.
import * as path from "node:path";
import { bundle } from "@remotion/bundler";
import { renderMedia, selectComposition } from "@remotion/renderer";
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
}

export type RenderVideo = (args: RenderVideoArgs) => Promise<void>;

// The image installs a pinned Chrome and names it here. Unset (a developer machine), Remotion
// uses the browser it downloads for its own version.
const browserExecutable = process.env.CHROME_EXECUTABLE || null;

export function remotionRenderer(serveUrl: string): RenderVideo {
  return async ({ spec, urls, outputLocation, onProgress }) => {
    const inputProps = { spec, urls };
    const composition = await selectComposition({
      serveUrl,
      id: COMPOSITION_ID,
      inputProps,
      browserExecutable,
    });
    await renderMedia({
      composition,
      serveUrl,
      inputProps,
      codec: "h264",
      outputLocation,
      browserExecutable,
      onProgress: ({ progress }) => onProgress(progress),
    });
  };
}
