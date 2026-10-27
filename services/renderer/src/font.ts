// The caption font is identified by its bytes. The layout stage measured lines with one
// file; drawing them with another (a replaced file on one side, a stale image on the
// other) would overflow boxes or leave gaps, and the names alone would not show it.
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { ApplicationFailure } from "@temporalio/activity";
import type { EditSpec } from "./edit-spec";

export function fontSha256(fontPath: string): string {
  return createHash("sha256").update(fs.readFileSync(fontPath)).digest("hex");
}

/** Refuses, for good, a spec laid out with a different font than `fontPath`. */
export function checkFont(spec: Pick<EditSpec, "font" | "font_sha256">, fontPath: string): void {
  const name = path.basename(fontPath);
  if (name !== spec.font) {
    throw ApplicationFailure.nonRetryable(
      `the spec was laid out with ${spec.font}, this renderer has ${name}`,
      "FontMismatch",
    );
  }
  if (fontSha256(fontPath) !== spec.font_sha256) {
    throw ApplicationFailure.nonRetryable(
      `${name} here is not the file the spec was laid out with (the hashes differ)`,
      "FontMismatch",
    );
  }
}
