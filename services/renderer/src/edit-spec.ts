// The EditSpec the layout stage writes and this service renders.
//
// The JSON Schema is generated from the Pydantic model (contracts/edit-spec.schema.json),
// so Python is the source of truth and this file only validates against it. The hash a
// render is keyed by is computed here over the canonical form of the spec, and must equal
// the one Python computes (localization.contracts.spec_hash).
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import Ajv2020 from "ajv/dist/2020";
import { ApplicationFailure } from "@temporalio/activity";

export interface BBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Cover {
  region: BBox;
  color: string;
  opacity_pct: number;
}

export interface Caption {
  segment_id: number;
  start_ms: number;
  end_ms: number;
  text: string;
  font_px: number;
}

export interface EditSpec {
  source_key: string;
  audio_key: string;
  audio_sha256: string;
  script_version: number;
  width: number;
  height: number;
  fps: number;
  duration_ms: number;
  cover: Cover | null;
  caption_box: BBox;
  captions: Caption[];
  font: string;
}

// <repo>/services/renderer/{src,dist}/edit-spec.* -> <repo>/contracts
export const EDIT_SPEC_SCHEMA_PATH = path.resolve(
  __dirname,
  "../../../contracts/edit-spec.schema.json",
);

export type SpecValidator = (value: unknown) => EditSpec;

export function createSpecValidator(schemaPath: string = EDIT_SPEC_SCHEMA_PATH): SpecValidator {
  const schema = JSON.parse(fs.readFileSync(schemaPath, "utf8"));
  const validate = new Ajv2020({ allErrors: true, strict: true }).compile(schema);
  return (value: unknown): EditSpec => {
    if (!validate(value)) {
      const why = (validate.errors ?? [])
        .map((e) => `${e.instancePath || "/"} ${e.message}`)
        .join("; ");
      // The same bytes fail the same way: retrying cannot help.
      throw ApplicationFailure.nonRetryable(`invalid EditSpec: ${why}`, "InvalidEditSpec");
    }
    return value as EditSpec;
  };
}

// Sorted keys, no whitespace, non-ASCII left as it is. A spec holds only strings and
// integers, which Python and JavaScript write identically.
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const members = Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`);
    return `{${members.join(",")}}`;
  }
  return JSON.stringify(value);
}

export function specSha256(spec: EditSpec): string {
  return createHash("sha256").update(canonicalJson(spec), "utf8").digest("hex");
}
