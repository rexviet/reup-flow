// Contract parity with Python. The fixtures are NOT committed: every run asks the Python
// model to produce them (`python -m localization.contracts.fixtures`), so a model change
// that the stored schema or the renderer does not follow fails here.
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  canonicalJson,
  createSpecValidator,
  specSha256,
  type Caption,
  type EditSpec,
} from "../src/edit-spec";

const LOCALIZATION = path.resolve(__dirname, "../../localization");
const validate = createSpecValidator();

let dir: string;
const names = ["with-cover", "no-cover", "awkward-text"];

beforeAll(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "edit-spec-fixtures-"));
  execFileSync(
    "uv",
    ["run", "--project", LOCALIZATION, "python", "-m", "localization.contracts.fixtures", dir],
    { cwd: LOCALIZATION, stdio: "pipe" },
  );
});

afterAll(() => fs.rmSync(dir, { recursive: true, force: true }));

function fixture(name: string): unknown {
  return JSON.parse(fs.readFileSync(path.join(dir, `${name}.json`), "utf8"));
}

describe("a spec Python wrote", () => {
  it.each(names)("%s is accepted by the schema", (name) => {
    expect(() => validate(fixture(name))).not.toThrow();
  });

  it.each(names)("%s hashes to what Python says", (name) => {
    const expected = fs.readFileSync(path.join(dir, `${name}.sha256`), "utf8").trim();
    expect(specSha256(validate(fixture(name)))).toBe(expected);
  });

  it("covers a spec with a cover, one without and one with awkward text", () => {
    expect((fixture("with-cover") as EditSpec).cover).not.toBeNull();
    expect((fixture("no-cover") as EditSpec).cover).toBeNull();
    expect((fixture("awkward-text") as EditSpec).captions[0].text).toContain("́");
  });
});

describe("a spec the schema must refuse", () => {
  type Loose = Record<string, unknown>;
  const good = () => structuredClone(fixture("with-cover")) as Loose;
  const caption = (s: Loose) => (s.captions as Caption[])[0] as unknown as Loose;
  const part = (s: Loose, name: string) => s[name] as Loose;

  const broken: Array<[string, (spec: Loose) => void]> = [
    ["an unknown field", (s) => (s.extra = 1)],
    ["a missing field", (s) => delete s.audio_sha256],
    ["a missing caption box", (s) => delete s.caption_box],
    ["a caption without a font size", (s) => delete caption(s).font_px],
    ["a font size of zero", (s) => (caption(s).font_px = 0)],
    ["a cover colour that is not #rrggbb", (s) => (part(s, "cover").color = "red")],
    ["a cover opacity above 100", (s) => (part(s, "cover").opacity_pct = 101)],
    ["a negative position", (s) => (part(s, "caption_box").x = -1)],
    ["a zero-sized frame", (s) => (s.width = 0)],
    ["a fractional frame rate", (s) => (s.fps = 29.97)],
    ["a frame wider than the limit", (s) => (s.width = 4097)],
    ["a frame taller than the limit", (s) => (s.height = 4097)],
    ["a frame rate above the limit", (s) => (s.fps = 121)],
    ["a video longer than the limit", (s) => (s.duration_ms = 15 * 60 * 1000 + 1)],
    ["each side within its limit but too many pixels together", (s) => Object.assign(s, { width: 3840, height: 2160 })],
    ["more captions than the limit", (s) => (s.captions = Array(2001).fill(caption(s)))],
    ["a missing font hash", (s) => delete s.font_sha256],
    ["a font hash that is not a sha256", (s) => (s.font_sha256 = "abc")],
  ];

  it.each(broken)("%s", (_label, mutate) => {
    const spec = good();
    mutate(spec);
    expect(() => validate(spec)).toThrow(/invalid EditSpec/);
  });

  it("accepts a video exactly at the limits", () => {
    const spec = good();
    Object.assign(spec, { width: 1920, height: 1088, fps: 120, duration_ms: 15 * 60 * 1000 });
    expect(() => validate(spec)).not.toThrow();
  });

  it("accepts a portrait video at the area limit", () => {
    const spec = good();
    Object.assign(spec, { width: 1088, height: 1920 });
    expect(() => validate(spec)).not.toThrow();
  });

  it("is refused for good, so the activity is not retried", () => {
    const spec = good();
    delete spec.font;
    expect.assertions(2);
    try {
      validate(spec);
    } catch (err) {
      const failure = err as { type?: string; nonRetryable?: boolean };
      expect(failure.type).toBe("InvalidEditSpec");
      expect(failure.nonRetryable).toBe(true);
    }
  });
});

describe("the canonical form", () => {
  it("does not depend on the order keys were written in", () => {
    expect(canonicalJson({ b: 1, a: { d: [2, 1], c: "x" } })).toBe(
      canonicalJson({ a: { c: "x", d: [2, 1] }, b: 1 }),
    );
  });

  it("changes when a value changes", () => {
    const spec = validate(fixture("with-cover"));
    const other = structuredClone(spec);
    other.captions[0].text += "!";
    expect(specSha256(other)).not.toBe(specSha256(spec));
  });
});
