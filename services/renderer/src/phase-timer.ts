// Where the time of one render goes. Each phase is measured with a monotonic clock and leaves
// two traces: one structured log line per render, and one child span per phase under the
// activity's span. Both carry durations and a short hash prefix only: never a URL, an object
// key, a caption or a file path.
import { performance } from "node:perf_hooks";
import {
  context,
  SpanStatusCode,
  trace,
  type Context,
  type Tracer,
} from "@opentelemetry/api";

export interface Clock {
  /** Monotonic milliseconds. */
  now(): number;
}

export const monotonicClock: Clock = { now: () => performance.now() };

export interface Phase {
  name: string;
  startMs: number;
  durationMs: number;
}

export function errorType(err: unknown): string {
  const type = (err as { type?: unknown } | null)?.type;
  if (typeof type === "string" && type) return type;
  return err instanceof Error ? err.name : "Error";
}

export class PhaseTimer {
  private readonly begun: number;
  private readonly wallBegun = Date.now();
  private readonly list: Phase[] = [];
  private readonly marked = new Map<string, number>();
  private failure: string | undefined;

  constructor(
    private readonly clock: Clock = monotonicClock,
    private readonly tracer: Tracer = trace.getTracer("render"),
    private readonly parent: Context = context.active(),
  ) {
    this.begun = clock.now();
  }

  /** Runs `fn` as the phase `name`. The phase is recorded whether it succeeds or fails. */
  async time<T>(name: string, fn: () => Promise<T>): Promise<T> {
    const start = this.clock.now();
    try {
      const out = await fn();
      this.record(name, start, this.clock.now());
      return out;
    } catch (err) {
      this.record(name, start, this.clock.now(), errorType(err));
      throw err;
    }
  }

  /** A phase whose start and end were seen from outside (monotonic times of this clock). */
  record(name: string, start: number, end: number, error?: string): void {
    const phase = { name, startMs: start - this.begun, durationMs: Math.max(0, end - start) };
    this.list.push(phase);
    if (error) this.failure = error;
    const wall = this.wallBegun + phase.startMs;
    const span = this.tracer.startSpan(`render.${name}`, { startTime: wall }, this.parent);
    span.setAttribute("render.duration_ms", Math.round(phase.durationMs));
    if (error) {
      span.setAttribute("error.type", error);
      span.setStatus({ code: SpanStatusCode.ERROR });
    }
    span.end(wall + phase.durationMs);
  }

  /** Names a moment (time since the render began), for the log only. */
  mark(name: string): void {
    if (!this.marked.has(name)) this.marked.set(name, this.clock.now() - this.begun);
  }

  has(name: string): boolean {
    return this.marked.has(name);
  }

  now(): number {
    return this.clock.now();
  }

  phases(): readonly Phase[] {
    return this.list;
  }

  /** The one line a render logs. `spec` is the hash prefix; nothing else identifies it. */
  summary(spec: string, error?: unknown): Record<string, unknown> {
    const durations: Record<string, number> = {};
    for (const p of this.list) durations[`${p.name}_ms`] = Math.round(p.durationMs);
    const marks: Record<string, number> = {};
    for (const [name, at] of this.marked) marks[`${name}_ms`] = Math.round(at);
    const failed = error === undefined ? this.failure : errorType(error);
    return {
      event: "render.timing",
      spec: spec.slice(0, 12),
      ...durations,
      ...marks,
      total_ms: Math.round(this.clock.now() - this.begun),
      ...(failed ? { error_type: failed } : {}),
    };
  }
}
