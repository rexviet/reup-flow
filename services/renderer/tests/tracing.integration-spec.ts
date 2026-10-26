// G3 on the Node side: the `render` activity runs inside the trace the workflow started,
// so its span sits in the same trace as the spans around it. The control is a worker
// built without the interceptor, which leaves no activity span in that trace.
import { context, trace } from "@opentelemetry/api";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { OpenTelemetryWorkflowClientInterceptor } from "@temporalio/interceptors-opentelemetry/lib/client";
import { Client } from "@temporalio/client";
import { TestWorkflowEnvironment } from "@temporalio/testing";
import { makeWorkflowExporter } from "@temporalio/interceptors-opentelemetry/lib/worker";
import { Worker } from "@temporalio/worker";
import { loadSettings } from "../src/config";
import { createProductionWorker } from "../src/worker";

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let env: TestWorkflowEnvironment;

beforeAll(async () => {
  provider.register();
  env = await TestWorkflowEnvironment.createLocal();
});

afterAll(async () => {
  await env?.teardown();
  await provider.shutdown();
});

const settings = loadSettings({
  CAPTION_FONT_PATH: "/assets/fonts/BeVietnamPro-SemiBold.ttf",
  RENDER_S3_ACCESS_KEY_ID: "x",
  RENDER_S3_SECRET_ACCESS_KEY: "y",
});

const render = async (n: number): Promise<number> => n;

/** Runs one workflow, started inside a parent span, and returns the trace id of that span. */
async function runInsideSpan(renderWorker: Worker, queue: string): Promise<string> {
  const workflowWorker = await Worker.create({
    connection: env.nativeConnection,
    taskQueue: queue,
    workflowsPath: require.resolve("./support/probe-workflow"),
    interceptors: { workflowModules: [require.resolve("./support/trace-interceptors")] },
    // Spans the interceptors make inside the workflow are handed out through this sink.
    sinks: { exporter: makeWorkflowExporter(new SimpleSpanProcessor(exporter), provider.resource) },
  });
  const client = new Client({
    connection: env.connection,
    interceptors: { workflow: [new OpenTelemetryWorkflowClientInterceptor()] },
  });
  const tracer = trace.getTracer("test");
  const parent = tracer.startSpan("test-run");
  const done = Promise.all([renderWorker.run(), workflowWorker.run()]);
  try {
    await context.with(trace.setSpan(context.active(), parent), () =>
      client.workflow.execute("renderMany", {
        taskQueue: queue,
        workflowId: `trace-${queue}-${Date.now()}`,
        args: [1],
      }),
    );
  } finally {
    parent.end();
    renderWorker.shutdown();
    workflowWorker.shutdown();
    await done;
  }
  return parent.spanContext().traceId;
}

function activitySpansIn(traceId: string) {
  return exporter
    .getFinishedSpans()
    .filter((s) => s.spanContext().traceId === traceId && s.name.includes("RunActivity"))
    .filter((s) => s.name.endsWith("render"));
}

describe("the render worker's tracing", () => {
  it("runs the render activity inside the trace that started the workflow", async () => {
    const worker = await createProductionWorker(env.nativeConnection, settings, { render });
    const traceId = await runInsideSpan(worker, "trace-with");
    expect(activitySpansIn(traceId)).toHaveLength(1);
  });

  it("control: a worker without the interceptor leaves no activity span in that trace", async () => {
    const worker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue: "render",
      activities: { render },
    });
    const traceId = await runInsideSpan(worker, "trace-without");
    expect(activitySpansIn(traceId)).toHaveLength(0);
  });
});
