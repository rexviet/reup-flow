import React from "react";
import { Composition } from "remotion";
import type { EditSpec } from "../edit-spec";
import { durationInFrames } from "../frames";
import { LocalizedVideo, type LocalizedVideoProps } from "./LocalizedVideo";

export const COMPOSITION_ID = "LocalizedVideo";

// Only what the bundle needs to register; every render passes the real spec as inputProps
// and `calculateMetadata` takes the size, rate and length from it.
const placeholder: EditSpec = {
  source_key: "",
  audio_key: "",
  audio_sha256: "",
  script_version: 0,
  width: 1080,
  height: 1920,
  fps: 30,
  duration_ms: 1000,
  cover: null,
  caption_box: { x: 0, y: 0, w: 1, h: 1 },
  captions: [],
  font: "",
};

export const RemotionRoot: React.FC = () => (
  <Composition
    id={COMPOSITION_ID}
    component={LocalizedVideo}
    width={placeholder.width}
    height={placeholder.height}
    fps={placeholder.fps}
    durationInFrames={durationInFrames(placeholder)}
    defaultProps={{ spec: placeholder, urls: { source: "", audio: "", font: "" } }}
    calculateMetadata={({ props }: { props: LocalizedVideoProps }) => ({
      width: props.spec.width,
      height: props.spec.height,
      fps: props.spec.fps,
      durationInFrames: durationInFrames(props.spec),
    })}
  />
);
