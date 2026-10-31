import { shortId, videoColor } from './videoColor';

/** A one-line speech bubble. Its text is the server's `detail` of a real event. */
export function Bubble({ text, awemeId }: { text: string; awemeId?: string }) {
  return (
    <span
      role="status"
      className="absolute -top-7 left-1 z-10 max-w-[18rem] truncate rounded-md bg-gray-900 px-2 py-1 text-xs text-white shadow motion-safe:animate-fade-in"
    >
      {awemeId && (
        <span className="mr-1 font-semibold" style={{ color: videoColor(awemeId) }}>
          …{shortId(awemeId)}
        </span>
      )}
      {text}
    </span>
  );
}
