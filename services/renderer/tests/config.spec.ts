import { ConfigError, loadSettings } from "../src/config";

const base = {
  CAPTION_FONT_PATH: "/assets/fonts/BeVietnamPro-SemiBold.ttf",
  RENDER_S3_ACCESS_KEY_ID: "renderer",
  RENDER_S3_SECRET_ACCESS_KEY: "secret-value",
};

describe("renderer settings", () => {
  it("render concurrency is one unless it is set", () => {
    expect(loadSettings(base).renderConcurrency).toBe(1);
    expect(loadSettings({ ...base, RENDER_CONCURRENCY: "" }).renderConcurrency).toBe(1);
    expect(loadSettings({ ...base, RENDER_CONCURRENCY: "3" }).renderConcurrency).toBe(3);
  });

  it.each(["0", "-1", "1.5", "many", "65"])("refuses RENDER_CONCURRENCY=%s", (value) => {
    expect(() => loadSettings({ ...base, RENDER_CONCURRENCY: value })).toThrow(ConfigError);
  });

  it.each(["CAPTION_FONT_PATH", "RENDER_S3_ACCESS_KEY_ID", "RENDER_S3_SECRET_ACCESS_KEY"])(
    "refuses to start without %s",
    (name) => {
      const env: Record<string, string> = { ...base };
      delete env[name];
      expect(() => loadSettings(env)).toThrow(name);
    },
  );

  it("says nothing of a secret when it refuses", () => {
    expect(() => loadSettings({ ...base, RENDER_CONCURRENCY: "x" })).not.toThrow(/secret-value/);
  });

  it("traces to Langfuse only when all three of its settings are there", () => {
    expect(loadSettings(base).langfuse).toBeNull();
    expect(loadSettings({ ...base, LANGFUSE_HOST: "http://langfuse-web:3000" }).langfuse).toBeNull();
    expect(
      loadSettings({
        ...base,
        LANGFUSE_HOST: "http://langfuse-web:3000",
        LANGFUSE_PUBLIC_KEY: "pk",
        LANGFUSE_SECRET_KEY: "sk",
      }).langfuse,
    ).toEqual({ host: "http://langfuse-web:3000", publicKey: "pk", secretKey: "sk" });
  });
});
