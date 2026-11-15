import type { S3Client } from '@aws-sdk/client-s3';
import { Readable } from 'node:stream';
import {
  createS3MediaReader,
  RangeNotSatisfiableError,
  S3MediaReader,
  sanitizeRange,
} from '../../src/dashboard/dashboard.media';
import { fixedEnv } from '../support/fixed-env';

function clientThatReturns(result: unknown, fail?: { name?: string; status?: number }): S3Client {
  return {
    send: jest.fn(async () => {
      if (fail) throw Object.assign(new Error('s3'), { name: fail.name ?? 'Error', $metadata: { httpStatusCode: fail.status } });
      return result;
    }),
  } as unknown as S3Client;
}

describe('S3MediaReader', () => {
  it('reads from the configured bucket only and forwards the range', async () => {
    const send = jest.fn(async () => ({
      Body: Readable.from(['x']),
      ContentLength: 100,
      ContentRange: 'bytes 0-99/1000',
      ContentType: 'video/mp4',
    }));
    const reader = new S3MediaReader({ send } as unknown as S3Client, 'reup-media');
    const object = await reader.open('output/a.mp4', 'bytes=0-99');
    expect(object).toMatchObject({ status: 206, contentRange: 'bytes 0-99/1000', contentLength: 100 });
    const command = (send.mock.calls[0] as unknown as [{ input: { Bucket: string; Key: string; Range: string } }])[0];
    expect(command.input).toEqual({ Bucket: 'reup-media', Key: 'output/a.mp4', Range: 'bytes=0-99' });
  });

  it('answers 200 without a range', async () => {
    const reader = new S3MediaReader(clientThatReturns({ Body: Readable.from(['x']), ContentLength: 1 }), 'b');
    expect((await reader.open('output/a.mp4'))?.status).toBe(200);
  });

  it.each([
    ['NoSuchKey', 404],
    ['NotFound', 404],
    ['AccessDenied', 403],
  ])('maps %s (%i) to not found, as MinIO answers 403 for a missing object', async (name, status) => {
    const reader = new S3MediaReader(clientThatReturns(undefined, { name, status }), 'b');
    expect(await reader.open('output/a.mp4')).toBeNull();
  });

  it('maps InvalidRange to a range error and rethrows anything else', async () => {
    await expect(
      new S3MediaReader(clientThatReturns(undefined, { name: 'InvalidRange', status: 416 }), 'b').open('k', 'bytes=9-'),
    ).rejects.toBeInstanceOf(RangeNotSatisfiableError);
    await expect(
      new S3MediaReader(clientThatReturns(undefined, { name: 'InternalError', status: 500 }), 'b').open('k'),
    ).rejects.toThrow('s3');
  });
});

describe('createS3MediaReader', () => {
  it('is null unless the storage backend is s3', () => {
    expect(createS3MediaReader(fixedEnv({ STORAGE_BACKEND: 'local' }))).toBeNull();
  });
  it('builds a reader for s3', () => {
    const env = fixedEnv({
      STORAGE_BACKEND: 's3',
      S3_ENDPOINT: 'http://minio:9000',
      S3_ACCESS_KEY_ID: 'a',
      S3_SECRET_ACCESS_KEY: 'b',
    });
    expect(createS3MediaReader(env)).toBeInstanceOf(S3MediaReader);
  });
});

describe('sanitizeRange', () => {
  it.each([
    ['bytes=0-99', 'bytes=0-99'],
    ['bytes=100-', 'bytes=100-'],
    ['bytes=-500', 'bytes=-500'],
    ['bytes=0-1,5-9', undefined],
    ['bytes=-', undefined],
    ['items=0-1', undefined],
    [undefined, undefined],
  ])('%p -> %p', (input, expected) => {
    expect(sanitizeRange(input)).toBe(expected);
  });
});
