import type { JobResponse } from '../api/types';

/**
 * phase-08-web-ui.md §8.2: TWO SEPARATE BARS. Do NOT merge these into a
 * single percentage.
 *
 * Before enumerate finishes, `total_videos` is NULL (see JobResponse's
 * comment in api/types.ts) -- the denominator for a combined percentage
 * literally does not exist yet. A merged bar would either divide by zero or
 * substitute a guess, and either way the number shown would JUMP DOWN the
 * moment enumerate finds more videos than the guess assumed. That is why the
 * enumerate bar renders indeterminate (no percentage at all, just a moving
 * stripe + the running count) until `total_videos` stops being null, and why
 * this component always renders two `role="progressbar"` elements, never one.
 *
 * DO NOT "clean this up" into a single bar. See phase-08-web-ui.md §8.2 and
 * the Risks table row "Ai đó gộp 2 thanh thành 1 %".
 */
export function ProgressBars({ job }: { job: JobResponse }) {
  const enumerateDeterminate = job.total_videos !== null;
  const enumeratePct = enumerateDeterminate ? 100 : undefined; // enumerate is binary: running (unknown target) or done (100%)

  const downloadDeterminate = job.total_videos !== null;
  // `total_videos === 0` (a profile enumerated to be empty) is a valid,
  // already-complete state, not "unknown" -- guard the division so it reads
  // as 100%, not NaN, instead of falling back to the indeterminate branch.
  const downloadPct = downloadDeterminate
    ? job.total_videos === 0
      ? 100
      : Math.round((job.downloaded_count / (job.total_videos as number)) * 100)
    : undefined;

  // F7 (phase-08-web-ui.md §8.4): a re-run against an already-complete
  // profile can land at enumerated_count === 0 for THIS job while
  // total_videos/downloaded_count already show the whole profile from a
  // PRIOR job (videos.aweme_id is a global key -- see F7 in plan.md §2).
  // Showing "340/340" immediately is correct, not a bug; this label is the
  // only thing standing between that correct behaviour and a confused
  // operator filing it as one. Gated on `status !== 'pending'` so a job that
  // simply hasn't started enumerating yet (enumerated_count is trivially 0
  // then too) doesn't show it prematurely.
  const showZeroNewLabel = job.enumerated_count === 0 && job.status !== 'pending';

  return (
    <div className="flex flex-col gap-2">
      <div>
        <div className="mb-1 flex items-center justify-between text-xs text-gray-600">
          <span>Đang quét: {job.enumerated_count} video</span>
          {showZeroNewLabel ? (
            <span className="rounded bg-blue-50 px-1.5 py-0.5 font-medium text-blue-700">0 video mới</span>
          ) : null}
        </div>
        <div
          data-testid="enumerate-bar"
          role="progressbar"
          aria-label="Tiến độ quét"
          {...(enumerateDeterminate
            ? { 'aria-valuenow': enumeratePct, 'aria-valuemin': 0, 'aria-valuemax': 100 }
            : { 'aria-valuetext': 'Đang quét, chưa biết tổng số video' })}
          className="h-2 w-full overflow-hidden rounded bg-gray-200"
        >
          {enumerateDeterminate ? (
            <div className="h-full rounded bg-blue-500" style={{ width: `${enumeratePct}%` }} />
          ) : (
            // Indeterminate: a moving stripe, not a fixed-width fill computed
            // from a ratio -- there is nothing to divide by yet.
            <div className="h-full w-1/3 animate-pulse rounded bg-blue-400" />
          )}
        </div>
      </div>

      <div>
        <div className="mb-1 text-xs text-gray-600">
          {downloadDeterminate ? `${job.downloaded_count} / ${job.total_videos}` : `${job.downloaded_count} / ?`}
        </div>
        <div
          data-testid="download-bar"
          role="progressbar"
          aria-label="Tiến độ tải"
          {...(downloadDeterminate
            ? { 'aria-valuenow': downloadPct, 'aria-valuemin': 0, 'aria-valuemax': 100 }
            : { 'aria-valuetext': 'Chưa biết tổng số video cần tải' })}
          className="h-2 w-full overflow-hidden rounded bg-gray-200"
        >
          {downloadDeterminate ? (
            <div className="h-full rounded bg-green-500" style={{ width: `${downloadPct}%` }} />
          ) : (
            <div className="h-full w-0 rounded bg-green-500" />
          )}
        </div>
      </div>
    </div>
  );
}
