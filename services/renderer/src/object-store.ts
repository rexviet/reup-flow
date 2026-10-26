// The renderer's view of S3: read the spec, source and audio, write the render. Its
// credential reaches `work/*` only (infra/minio/init.sh), so every key it touches is there.
import * as fs from "node:fs";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import {
  GetObjectCommand,
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
  type S3ClientConfig,
} from "@aws-sdk/client-s3";
import { ApplicationFailure } from "@temporalio/activity";
import type { RendererSettings } from "./config";

export interface ObjectStore {
  getBytes(key: string): Promise<Buffer>;
  download(key: string, destination: string): Promise<void>;
  exists(key: string): Promise<boolean>;
  uploadFile(key: string, file: string, contentType: string): Promise<void>;
}

interface AwsError {
  name?: string;
  Code?: string;
  $metadata?: { httpStatusCode?: number };
}

function isMissing(err: unknown): boolean {
  const e = err as AwsError;
  return (
    e.name === "NoSuchKey" ||
    e.name === "NotFound" ||
    e.Code === "NoSuchKey" ||
    e.$metadata?.httpStatusCode === 404
  );
}

export class S3ObjectStore implements ObjectStore {
  constructor(
    private readonly client: S3Client,
    private readonly bucket: string,
  ) {}

  static fromSettings(settings: RendererSettings["s3"]): S3ObjectStore {
    const config: S3ClientConfig = {
      endpoint: settings.endpoint,
      region: settings.region,
      forcePathStyle: settings.forcePathStyle,
      credentials: {
        accessKeyId: settings.accessKeyId,
        secretAccessKey: settings.secretAccessKey,
      },
    };
    return new S3ObjectStore(new S3Client(config), settings.bucket);
  }

  private async body(key: string): Promise<Readable> {
    try {
      const out = await this.client.send(new GetObjectCommand({ Bucket: this.bucket, Key: key }));
      return out.Body as Readable;
    } catch (err) {
      // A missing input cannot be brought back by retrying the render: the workflow
      // re-runs the stage that made it.
      if (isMissing(err)) {
        throw ApplicationFailure.nonRetryable(`artifact ${key} is missing`, "ArtifactMissing");
      }
      throw err;
    }
  }

  async getBytes(key: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of await this.body(key)) chunks.push(Buffer.from(chunk));
    return Buffer.concat(chunks);
  }

  async download(key: string, destination: string): Promise<void> {
    await pipeline(await this.body(key), fs.createWriteStream(destination));
  }

  async exists(key: string): Promise<boolean> {
    try {
      await this.client.send(new HeadObjectCommand({ Bucket: this.bucket, Key: key }));
      return true;
    } catch (err) {
      if (isMissing(err)) return false;
      throw err;
    }
  }

  async uploadFile(key: string, file: string, contentType: string): Promise<void> {
    await this.client.send(
      new PutObjectCommand({
        Bucket: this.bucket,
        Key: key,
        Body: fs.createReadStream(file),
        ContentLength: fs.statSync(file).size,
        ContentType: contentType,
      }),
    );
  }
}
