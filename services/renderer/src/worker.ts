// The render worker: queue `render`, at most RENDER_CONCURRENCY renders at a time.
//
//   node dist/worker.js
//
// Chromium and the compositor are heavy, and a worker that starts as many renders as
// tasks arrive would use all the memory of the machine for them; the limit is applied
// in `createProductionWorker`, the one place a Worker is built.
import { NativeConnection, Worker } from "@temporalio/worker";
import { createRenderActivity, RenderInvocations } from "./activities/render";
import { ConfigError, loadSettings, type RendererSettings } from "./config";
import { S3ObjectStore } from "./object-store";
import { bundleComposition, remotionRenderer } from "./remotion-render";
import { initTracing, tracingInterceptors } from "./tracing";

export const RENDER_QUEUE = "render";
// In-flight renders get this long to finish when the worker is told to stop; compose's
// stop_grace_period is longer, so a deploy does not kill a render halfway.
export const GRACEFUL_SHUTDOWN = "10 minutes";

export type RenderActivities = Record<string, (...args: never[]) => Promise<unknown>>;

/** The one place a Worker is built: the queue, the concurrency limit from settings and
 * the tracing interceptor. Whatever serves `activities` gets all three. */
export function createProductionWorker(
  connection: NativeConnection,
  settings: Pick<RendererSettings, "temporalNamespace" | "renderConcurrency">,
  activities: RenderActivities,
): Promise<Worker> {
  return Worker.create({
    connection,
    namespace: settings.temporalNamespace,
    taskQueue: RENDER_QUEUE,
    activities,
    interceptors: tracingInterceptors(),
    maxConcurrentActivityTaskExecutions: settings.renderConcurrency,
    shutdownGraceTime: GRACEFUL_SHUTDOWN,
  });
}

async function main(): Promise<void> {
  const settings = loadSettings();
  const tracing = initTracing(settings, "renderer-worker");
  const bundleUrl = await bundleComposition();
  const render = createRenderActivity({
    store: S3ObjectStore.fromSettings(settings.s3),
    renderVideo: remotionRenderer(bundleUrl),
    fontPath: settings.captionFontPath,
    invocations: new RenderInvocations(),
  });
  const connection = await NativeConnection.connect({ address: settings.temporalAddress });
  const worker = await createProductionWorker(connection, settings, { render });
  for (const signal of ["SIGTERM", "SIGINT"] as const) {
    process.once(signal, () => worker.shutdown());
  }
  console.log(`render worker ready (concurrency ${settings.renderConcurrency})`);
  await worker.run();
  await connection.close();
  await tracing?.shutdown();
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err instanceof ConfigError ? err.message : err);
    process.exit(1);
  });
}
