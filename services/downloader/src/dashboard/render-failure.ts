/**
 * A failed final render explained for the admin. The worker stores the raw reason in
 * `localization.final_renders.error` (review_worker/final_render.py): a Vietnamese sentence it
 * wrote itself, or `<ExceptionName>: <message>` for anything unexpected. The raw text stays on the
 * detail page under "Chi tiết kỹ thuật"; this is what the admin reads first.
 */

/**
 * `outdated`: the render machine runs old code; `stale`: the review changed after approval, so
 * rendering again cannot help; `timeout`: it ran too long; `unknown`: anything else.
 */
export type RenderFailureKind = 'outdated' | 'stale' | 'timeout' | 'unknown';

export interface RenderFailure {
  kind: RenderFailureKind;
  reason: string;
  action: string;
}

export const UPDATE_RENDER_MACHINE = 'Trên máy đó, bấm đúp file cap-nhat-may-render.bat trong thư mục reup-flow.';

const RULES: ReadonlyArray<{ kind: RenderFailureKind; test: RegExp }> = [
  // A manifest field the machine's code does not know (pydantic `extra_forbidden`).
  { kind: 'outdated', test: /ValidationError|extra_forbidden|Extra inputs are not permitted|ReviewProjectManifest/ },
  // RenderRejected / HEAD mismatch, and an approval that changed while rendering.
  { kind: 'stale', test: /RenderRejected|HEAD|đã đổi sau khi duyệt|duyệt lại bản mới|Quyết định duyệt đã đổi/ },
  { kind: 'timeout', test: /TimeoutError|timed? ?out|quá thời gian|Render quá \d+ phút/i },
];

const TEXT: Record<RenderFailureKind, Omit<RenderFailure, 'kind'>> = {
  outdated: {
    reason: 'Máy render đang chạy bản cũ, không đọc được bản review mới.',
    action: `Cập nhật máy render rồi bấm Render lại. ${UPDATE_RENDER_MACHINE}`,
  },
  stale: {
    reason: 'Bản review đã đổi sau khi duyệt nên không render bản cũ nữa.',
    action: 'Mở Studio xem bản mới rồi bấm Duyệt lại.',
  },
  timeout: {
    reason: 'Render quá thời gian cho phép nên đã dừng.',
    action: 'Bấm Render lại.',
  },
  unknown: {
    reason: 'Render lỗi.',
    action: 'Bấm Render lại; nếu vẫn lỗi, xem chi tiết kỹ thuật.',
  },
};

export function renderFailureOf(error: string | null | undefined): RenderFailure {
  const kind = (error && RULES.find((r) => r.test.test(error))?.kind) || 'unknown';
  return { kind, ...TEXT[kind] };
}
