// Time to frames. Pure, and free of Node imports, because the Remotion bundle (which
// runs in a browser) imports it too.

// The last frame is a partial one when the duration is not a whole number of frames,
// and is still drawn.
export function durationInFrames(spec: { duration_ms: number; fps: number }): number {
  return Math.max(1, Math.ceil((spec.duration_ms * spec.fps) / 1000));
}

export function frameAt(ms: number, fps: number): number {
  return Math.round((ms * fps) / 1000);
}
