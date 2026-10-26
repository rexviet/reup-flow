// A workflow that calls `render` on its queue several times at once, like the real
// workflow will. Used by tests that drive the production worker factory.
import { proxyActivities } from "@temporalio/workflow";

const { render } = proxyActivities<{ render(n: number): Promise<number> }>({
  taskQueue: "render",
  startToCloseTimeout: "1 minute",
});

export async function renderMany(count: number): Promise<number[]> {
  return Promise.all(Array.from({ length: count }, (_, i) => render(i)));
}
