import {
  AbortMultipartUploadCommand,
  DeleteObjectCommand,
  GetObjectCommand,
  ListMultipartUploadsCommand,
  ListObjectsV2Command,
  S3Client,
} from '@aws-sdk/client-s3';
import { randomUUID } from 'node:crypto';

/**
 * Connection settings for the S3 integration suites, read from the same
 * variables the service reads. No defaults for the endpoint or credentials: a
 * suite that silently fell back to something would be testing something else.
 */
export interface S3TestEnv {
  endpoint: string;
  region: string;
  bucket: string;
  accessKeyId: string;
  secretAccessKey: string;
}

export function readS3TestEnv(source: NodeJS.ProcessEnv = process.env): S3TestEnv {
  const missing = ['S3_ENDPOINT', 'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY'].filter(
    (name) => !source[name],
  );
  if (missing.length > 0) {
    throw new Error(
      `S3 integration tests need ${missing.join(', ')} (the downloader's MinIO user). ` +
        'MinIO publishes no S3 port, so point S3_ENDPOINT at a loopback proxy onto the compose network.',
    );
  }
  return {
    endpoint: source.S3_ENDPOINT as string,
    region: source.S3_REGION ?? 'us-east-1',
    bucket: source.S3_BUCKET ?? 'reup-media',
    accessKeyId: source.S3_ACCESS_KEY_ID as string,
    secretAccessKey: source.S3_SECRET_ACCESS_KEY as string,
  };
}

export function makeS3Client(env: S3TestEnv): S3Client {
  return new S3Client({
    endpoint: env.endpoint,
    region: env.region,
    forcePathStyle: true,
    credentials: { accessKeyId: env.accessKeyId, secretAccessKey: env.secretAccessKey },
  });
}

/** A key prefix no other run shares, under the only prefix the credential may write. */
export function uniqueTestPrefix(label: string): string {
  return `source/it-${label}-${randomUUID()}/`;
}

/** Unfinished multipart uploads under `prefix`, read straight from the store. */
export async function listUploads(
  client: S3Client,
  bucket: string,
  prefix: string,
): Promise<Array<{ key: string; uploadId: string }>> {
  // Filtered here, not with `Prefix`: MinIO returns nothing for a prefix that
  // is not a complete key, which would make every "no upload left" assertion
  // pass vacuously.
  const found: Array<{ key: string; uploadId: string }> = [];
  let keyMarker: string | undefined;
  let uploadIdMarker: string | undefined;
  for (;;) {
    const res = await client.send(
      new ListMultipartUploadsCommand({
        Bucket: bucket,
        KeyMarker: keyMarker,
        UploadIdMarker: uploadIdMarker,
      }),
    );
    for (const u of res.Uploads ?? []) {
      if (u.Key?.startsWith(prefix)) found.push({ key: u.Key, uploadId: u.UploadId as string });
    }
    if (!res.IsTruncated) return found;
    keyMarker = res.NextKeyMarker;
    uploadIdMarker = res.NextUploadIdMarker;
  }
}

/** Object keys under `prefix`, read straight from the store. */
export async function listObjectKeys(
  client: S3Client,
  bucket: string,
  prefix: string,
): Promise<string[]> {
  const res = await client.send(new ListObjectsV2Command({ Bucket: bucket, Prefix: prefix }));
  return (res.Contents ?? []).map((o) => o.Key as string);
}

export async function getObjectBytes(client: S3Client, bucket: string, key: string): Promise<Buffer> {
  const res = await client.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
  const body = res.Body as { transformToByteArray(): Promise<Uint8Array> };
  return Buffer.from(await body.transformToByteArray());
}

/** Removes everything a suite left under its own prefix: objects and unfinished uploads. */
export async function wipePrefix(client: S3Client, bucket: string, prefix: string): Promise<void> {
  for (const u of await listUploads(client, bucket, prefix)) {
    await client.send(
      new AbortMultipartUploadCommand({ Bucket: bucket, Key: u.key, UploadId: u.uploadId }),
    );
  }
  for (const key of await listObjectKeys(client, bucket, prefix)) {
    await client.send(new DeleteObjectCommand({ Bucket: bucket, Key: key }));
  }
}
