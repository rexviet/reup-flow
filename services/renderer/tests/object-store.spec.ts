import { S3Client } from "@aws-sdk/client-s3";
import { S3ObjectStore } from "../src/object-store";

// A client whose every call fails the way the store under test would answer.
function storeFailing(status: number, name: string): S3ObjectStore {
  const client = {
    send: async () => {
      throw Object.assign(new Error(name), { name, $metadata: { httpStatusCode: status } });
    },
  } as unknown as S3Client;
  return new S3ObjectStore(client, "bucket");
}

describe("the renderer's object store", () => {
  it("says an object is missing when the store answers 404", async () => {
    expect(await storeFailing(404, "NotFound").exists("work/1/r1/x")).toBe(false);
  });

  it("says an object is missing when the store answers 403, as AWS S3 does for a key it may not list", async () => {
    expect(await storeFailing(403, "Forbidden").exists("work/1/r1/x")).toBe(false);
  });

  it("does not hide other failures behind \"missing\"", async () => {
    await expect(storeFailing(500, "InternalError").exists("work/1/r1/x")).rejects.toThrow();
    await expect(storeFailing(503, "SlowDown").exists("work/1/r1/x")).rejects.toThrow();
  });

  it("does not report a refused read as a missing artifact", async () => {
    await expect(storeFailing(403, "AccessDenied").getBytes("work/1/r1/x")).rejects.not.toMatchObject({
      type: "ArtifactMissing",
    });
  });

  it("reports a read of a missing object as a missing artifact, for good", async () => {
    await expect(storeFailing(404, "NoSuchKey").getBytes("work/1/r1/x")).rejects.toMatchObject({
      type: "ArtifactMissing",
      nonRetryable: true,
    });
  });
});
