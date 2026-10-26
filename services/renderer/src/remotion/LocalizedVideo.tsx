// The one composition: the source picture with its own sound removed, the dubbed track
// over it, a cover over the burned-in subtitle and one Sequence per caption. No zoom, no
// colour work, nothing that is not in the spec.
import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  Audio,
  OffthreadVideo,
  Sequence,
  cancelRender,
  continueRender,
  delayRender,
  useVideoConfig,
} from "remotion";
import type { Caption, EditSpec } from "../edit-spec";
import { frameAt } from "../frames";

// A type, not an interface: Remotion wants props assignable to Record<string, unknown>.
export type LocalizedVideoProps = {
  spec: EditSpec;
  urls: { source: string; audio: string; font: string };
};

export const FONT_FAMILY = "CaptionFont";
export const LINE_HEIGHT = 1.25;
const TEXT_COLOR = "#ffffff";

function useCaptionFont(url: string): void {
  const [handle] = useState(() => delayRender("caption font"));
  useEffect(() => {
    const face = new FontFace(FONT_FAMILY, `url(${url})`);
    face
      .load()
      .then((loaded) => {
        (document.fonts as unknown as { add(face: FontFace): void }).add(loaded);
        continueRender(handle);
      })
      .catch((err) => cancelRender(err));
  }, [handle, url]);
}

function rgba(hex: string, opacityPct: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${opacityPct / 100})`;
}

const CaptionText: React.FC<{ caption: Caption }> = ({ caption }) => (
  <div
    data-caption={caption.segment_id}
    style={{
      fontFamily: FONT_FAMILY,
      fontSize: caption.font_px,
      lineHeight: LINE_HEIGHT,
      color: TEXT_COLOR,
      textAlign: "center",
      textShadow: "0 2px 6px rgba(0, 0, 0, 0.75)",
    }}
  >
    {caption.text.split("\n").map((line, index) => (
      // The layout stage chose where each line ends; the renderer must not rewrap.
      <div key={index} style={{ whiteSpace: "pre" }}>
        {line}
      </div>
    ))}
  </div>
);

export const LocalizedVideo: React.FC<LocalizedVideoProps> = ({ spec, urls }) => {
  const { fps } = useVideoConfig();
  useCaptionFont(urls.font);
  const box = spec.caption_box;
  return (
    <AbsoluteFill style={{ backgroundColor: "#000000" }}>
      <OffthreadVideo
        muted
        src={urls.source}
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
      />
      <Audio src={urls.audio} />
      {spec.cover ? (
        <AbsoluteFill
          data-cover
          style={{
            left: spec.cover.region.x,
            top: spec.cover.region.y,
            width: spec.cover.region.w,
            height: spec.cover.region.h,
            backgroundColor: rgba(spec.cover.color, spec.cover.opacity_pct),
          }}
        />
      ) : null}
      {spec.captions.map((caption, index) => {
        const from = frameAt(caption.start_ms, fps);
        const length = Math.max(1, frameAt(caption.end_ms, fps) - from);
        return (
          <Sequence key={index} from={from} durationInFrames={length} layout="none">
            <AbsoluteFill
              style={{
                left: box.x,
                top: box.y,
                width: box.w,
                height: box.h,
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <CaptionText caption={caption} />
            </AbsoluteFill>
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
