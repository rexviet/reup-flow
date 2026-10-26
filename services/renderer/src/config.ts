// Settings, read from the environment once at startup. A missing required value
// fails here, at boot, and not on the first video.
import * as path from "node:path";

export interface RendererSettings {
  temporalAddress: string;
  temporalNamespace: string;
  renderConcurrency: number;
  captionFontPath: string;
  s3: {
    endpoint: string | undefined;
    region: string;
    bucket: string;
    forcePathStyle: boolean;
    accessKeyId: string;
    secretAccessKey: string;
  };
  langfuse: { host: string; publicKey: string; secretKey: string } | null;
}

export class ConfigError extends Error {}

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name];
  if (value === undefined || value === "") throw new ConfigError(`${name} is not set`);
  return value;
}

export function parseConcurrency(raw: string | undefined): number {
  if (raw === undefined || raw === "") return 1;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > 64) {
    throw new ConfigError("RENDER_CONCURRENCY must be an integer from 1 to 64");
  }
  return value;
}

export function loadSettings(env: NodeJS.ProcessEnv = process.env): RendererSettings {
  const host = env.LANGFUSE_HOST;
  const publicKey = env.LANGFUSE_PUBLIC_KEY;
  const secretKey = env.LANGFUSE_SECRET_KEY;
  return {
    temporalAddress: env.TEMPORAL_ADDRESS || "localhost:7233",
    temporalNamespace: env.TEMPORAL_NAMESPACE || "default",
    renderConcurrency: parseConcurrency(env.RENDER_CONCURRENCY),
    captionFontPath: path.resolve(required(env, "CAPTION_FONT_PATH")),
    s3: {
      endpoint: env.S3_ENDPOINT || undefined,
      region: env.S3_REGION || "us-east-1",
      bucket: env.S3_BUCKET || "reup-media",
      forcePathStyle: (env.S3_FORCE_PATH_STYLE ?? "true") !== "false",
      accessKeyId: required(env, "RENDER_S3_ACCESS_KEY_ID"),
      secretAccessKey: required(env, "RENDER_S3_SECRET_ACCESS_KEY"),
    },
    langfuse: host && publicKey && secretKey ? { host, publicKey, secretKey } : null,
  };
}
