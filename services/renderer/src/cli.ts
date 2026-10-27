// Renders one spec from local files, with the same composition and server as the worker.
// Used by the font parity test, which measures real rendered pixels.
//
//   node dist/cli.js --spec spec.json --source in.mp4 --audio a.wav --font f.ttf --out out.mp4
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { startAssetServer } from "./asset-server";
import { createSpecValidator } from "./edit-spec";
import { checkFont } from "./font";
import { bundleComposition, remotionRenderer } from "./remotion-render";

function arg(name: string): string {
  const index = process.argv.indexOf(`--${name}`);
  const value = index === -1 ? undefined : process.argv[index + 1];
  if (!value) throw new Error(`missing --${name}`);
  return value;
}

async function main(): Promise<void> {
  const spec = createSpecValidator()(JSON.parse(fs.readFileSync(arg("spec"), "utf8")));
  checkFont(spec, arg("font"));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "render-cli-"));
  const server = await (async () => {
    fs.copyFileSync(arg("source"), path.join(dir, "source.mp4"));
    fs.copyFileSync(arg("audio"), path.join(dir, "audio.wav"));
    fs.copyFileSync(arg("font"), path.join(dir, "font.ttf"));
    return startAssetServer(dir);
  })();
  try {
    const render = remotionRenderer(await bundleComposition());
    await render({
      spec,
      urls: {
        source: server.url("source.mp4"),
        audio: server.url("audio.wav"),
        font: server.url("font.ttf"),
      },
      outputLocation: path.resolve(arg("out")),
      onProgress: () => undefined,
    });
  } finally {
    await server.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
