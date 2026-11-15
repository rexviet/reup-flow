// G3, Node side: a worker that serves a fake `render` activity on queue `render`
// and exports its spans to Langfuse over OTLP. The trace context arrives in the
// Temporal activity headers, so the span should join the Python run's trace.
import { Context } from "@temporalio/activity";
import { Worker, NativeConnection } from "@temporalio/worker";
import { OpenTelemetryActivityInboundInterceptor } from "@temporalio/interceptors-opentelemetry/lib/worker";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { Resource } from "@opentelemetry/resources";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

const address = process.env.TEMPORAL_ADDRESS ?? "temporal:7233";
const namespace = process.env.TEMPORAL_NAMESPACE ?? "default";
const langfuse = process.env.LANGFUSE_HOST ?? "http://langfuse-web:3000";
const auth = Buffer.from(
  `${process.env.LANGFUSE_PUBLIC_KEY}:${process.env.LANGFUSE_SECRET_KEY}`,
).toString("base64");

// Simple (synchronous) processor on purpose: the spike measures span counts, and
// a batch processor would turn "spans lost to the batch window" into noise.
const provider = new NodeTracerProvider({
  resource: new Resource({ "service.name": "render-worker" }),
  spanProcessors: [
    new SimpleSpanProcessor(
      new OTLPTraceExporter({
        url: `${langfuse}/api/public/otel/v1/traces`,
        headers: { Authorization: `Basic ${auth}`, "x-langfuse-ingestion-version": "4" },
      }),
    ),
  ],
});
provider.register();

async function render(specSha: string): Promise<string> {
  // A real render takes seconds. Keep it short but non-zero.
  await new Promise((r) => setTimeout(r, 300));
  Context.current().log.info("fake render done", { specSha });
  return `render-${specSha}.mp4`;
}

async function main() {
  const connection = await NativeConnection.connect({ address });
  const worker = await Worker.create({
    connection,
    namespace,
    taskQueue: "render",
    activities: { render },
    interceptors: {
      activity: [(ctx) => ({ inbound: new OpenTelemetryActivityInboundInterceptor(ctx) })],
    },
  });
  console.log("render worker ready");
  await worker.run();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
