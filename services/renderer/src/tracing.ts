// Tracing for the render worker: spans go to Langfuse over OTLP, and the Temporal activity
// interceptor continues the trace the Python workflow started, so the `render` span sits
// in the same trace as the spans of the stages around it.
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { Resource } from "@opentelemetry/resources";
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OpenTelemetryActivityInboundInterceptor } from "@temporalio/interceptors-opentelemetry/lib/worker";
import type { WorkerInterceptors } from "@temporalio/worker";
import type { RendererSettings } from "./config";

const OTLP_PATH = "/api/public/otel/v1/traces";

/** What the Worker is built with so every activity runs inside the propagated trace. */
export function tracingInterceptors(): WorkerInterceptors {
  return {
    activity: [(ctx) => ({ inbound: new OpenTelemetryActivityInboundInterceptor(ctx) })],
  };
}

export function langfuseHeaders(langfuse: { publicKey: string; secretKey: string }) {
  const token = Buffer.from(`${langfuse.publicKey}:${langfuse.secretKey}`).toString("base64");
  return { Authorization: `Basic ${token}`, "x-langfuse-ingestion-version": "4" };
}

export interface Tracing {
  shutdown(): Promise<void>;
}

/** Starts the exporter. Returns null, and says so, when Langfuse is not configured:
 * spans are then dropped, never sent anywhere else. */
export function initTracing(settings: RendererSettings, serviceName: string): Tracing | null {
  if (settings.langfuse === null) {
    console.warn("LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY unset; tracing off");
    return null;
  }
  const sdk = new NodeSDK({
    resource: new Resource({ "service.name": serviceName }),
    traceExporter: new OTLPTraceExporter({
      url: `${settings.langfuse.host.replace(/\/+$/, "")}${OTLP_PATH}`,
      headers: langfuseHeaders(settings.langfuse),
    }),
  });
  sdk.start();
  return { shutdown: () => sdk.shutdown() };
}
