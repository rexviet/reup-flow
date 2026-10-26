// AC6 for the `render` queue: with RENDER_CONCURRENCY=1 two renders never overlap, however
// many the workflow starts at once. The control shows this test can see overlap: with a
// limit of 3 the same workflow gets three running together.
import { TestWorkflowEnvironment } from "@temporalio/testing";
import { Worker } from "@temporalio/worker";
import { loadSettings } from "../src/config";
import { createProductionWorker } from "../src/worker";

let env: TestWorkflowEnvironment;

beforeAll(async () => {
  env = await TestWorkflowEnvironment.createLocal();
});

afterAll(async () => {
  await env?.teardown();
});

const baseEnv = {
  CAPTION_FONT_PATH: "/assets/fonts/BeVietnamPro-SemiBold.ttf",
  RENDER_S3_ACCESS_KEY_ID: "x",
  RENDER_S3_SECRET_ACCESS_KEY: "y",
};

async function peakOverlap(concurrency: string, starts: number): Promise<number> {
  const settings = loadSettings({ ...baseEnv, RENDER_CONCURRENCY: concurrency });
  let running = 0;
  let peak = 0;
  const render = async (n: number): Promise<number> => {
    running += 1;
    peak = Math.max(peak, running);
    // Long enough that every activity task has been delivered while the first runs.
    await new Promise((resolve) => setTimeout(resolve, 600));
    running -= 1;
    return n;
  };
  const renderWorker = await createProductionWorker(env.nativeConnection, settings, { render });
  const workflowWorker = await Worker.create({
    connection: env.nativeConnection,
    taskQueue: `probe-workflows-${concurrency}`,
    workflowsPath: require.resolve("./support/probe-workflow"),
  });
  await Promise.all([
    (async () => {
      const done = Promise.all([renderWorker.run(), workflowWorker.run()]);
      try {
        const results = await env.client.workflow.execute("renderMany", {
          taskQueue: `probe-workflows-${concurrency}`,
          workflowId: `renderMany-${concurrency}-${Date.now()}`,
          args: [starts],
        });
        expect(results).toHaveLength(starts);
      } finally {
        renderWorker.shutdown();
        workflowWorker.shutdown();
        await done;
      }
    })(),
  ]);
  return peak;
}

describe("the render queue", () => {
  it("runs one render at a time when RENDER_CONCURRENCY is 1", async () => {
    expect(await peakOverlap("1", 3)).toBe(1);
  });

  it("control: with RENDER_CONCURRENCY at 3 the same three renders overlap", async () => {
    expect(await peakOverlap("3", 3)).toBe(3);
  });
});
