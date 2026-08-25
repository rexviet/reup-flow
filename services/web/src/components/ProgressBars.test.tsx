import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ProgressBars } from './ProgressBars';
import type { JobResponse } from '../api/types';

/**
 * Fixtures mirror the REAL shape jobs.mapper.ts's `toJobResponse` produces at
 * each stage of a job's life -- not an easier world. In particular:
 *   - mid-enumerate: `total_videos` is `null` (finishEnumerate hasn't run).
 *   - re-run of a fully-downloaded profile: `enumerated_count` can land back
 *     at 0 for a job that never advanced past its initial checkpoint value
 *     (F7 -- see phase-08-web-ui.md §8.4), while `total_videos` /
 *     `downloaded_count` already reflect the whole profile from a PRIOR job,
 *     because `videos` is keyed globally on `aweme_id`.
 */
function job(overrides: Partial<JobResponse>): JobResponse {
  return {
    job_id: 'j1',
    sec_user_id: 'sec1',
    status: 'enumerating',
    total_videos: null,
    enumerated_count: 0,
    downloaded_count: 0,
    failed_count: 0,
    max_cursor: 0,
    error: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('ProgressBars -- enumerate bar', () => {
  it('is indeterminate while total_videos is NULL, per §8.2: the denominator does not exist yet', () => {
    render(<ProgressBars job={job({ status: 'enumerating', total_videos: null, enumerated_count: 340 })} />);
    const bar = screen.getByTestId('enumerate-bar');
    // Indeterminate means no fixed-width fill computed from a ratio -- there
    // is nothing to divide by. Asserted via aria-valuenow being ABSENT
    // (aria-valuenow present would imply a determinate percentage), not by a
    // CSS class name, so a future restyle can't accidentally make this pass.
    expect(bar).toHaveAttribute('aria-valuetext');
    expect(bar).not.toHaveAttribute('aria-valuenow');
    expect(screen.getByText(/Đang quét: 340 video/)).toBeInTheDocument();
  });

  it('becomes determinate once total_videos has a value (enumerate finished)', () => {
    render(<ProgressBars job={job({ status: 'downloading', total_videos: 340, enumerated_count: 340 })} />);
    const bar = screen.getByTestId('enumerate-bar');
    expect(bar).toHaveAttribute('aria-valuenow', '100');
  });
});

describe('ProgressBars -- download bar', () => {
  it('shows "downloaded / total" only once total_videos is known', () => {
    render(<ProgressBars job={job({ status: 'downloading', total_videos: 340, downloaded_count: 127 })} />);
    expect(screen.getByText('127 / 340')).toBeInTheDocument();
    expect(screen.getByTestId('download-bar')).toHaveAttribute('aria-valuenow', String(Math.round((127 / 340) * 100)));
  });

  it('shows "0 / 0" (not the NULL placeholder) for a profile enumerated to be empty', () => {
    render(<ProgressBars job={job({ status: 'completed', total_videos: 0, downloaded_count: 0 })} />);
    expect(screen.getByText('0 / 0')).toBeInTheDocument();
    expect(screen.getByTestId('download-bar')).toHaveAttribute('aria-valuenow', '100');
  });

  it('shows a placeholder, not a bogus ratio, while total_videos is still NULL', () => {
    render(<ProgressBars job={job({ status: 'enumerating', total_videos: null, downloaded_count: 0 })} />);
    expect(screen.getByText('0 / ?')).toBeInTheDocument();
    expect(screen.getByTestId('download-bar')).not.toHaveAttribute('aria-valuenow');
  });

  it('the two bars are never combined into a single percentage (§8.2 / risk register)', () => {
    render(<ProgressBars job={job({ status: 'downloading', total_videos: 340, downloaded_count: 127, enumerated_count: 340 })} />);
    // Two distinct progressbar roles must exist -- a single merged bar would
    // fail this count.
    expect(screen.getAllByRole('progressbar')).toHaveLength(2);
  });
});

describe('ProgressBars -- F7 "0 video mới" label (§8.4)', () => {
  it('shows "0 video mới" when a re-run enumerates nothing new against an already-complete profile', () => {
    // This is the literal F7 scenario: job 2 on a profile job 1 already
    // finished. `enumerated_count` lands at 0 for this job while
    // total_videos/downloaded_count already read 340/340 from global
    // `videos` state. 340/340 immediately is CORRECT, not a bug -- the label
    // exists so nobody mistakes it for one.
    render(
      <ProgressBars
        job={job({ status: 'completed', total_videos: 340, downloaded_count: 340, enumerated_count: 0 })}
      />,
    );
    expect(screen.getByText('340 / 340')).toBeInTheDocument();
    expect(screen.getByText('0 video mới')).toBeInTheDocument();
  });

  it('does NOT show the label for a fresh job that has not started enumerating yet', () => {
    render(<ProgressBars job={job({ status: 'pending', total_videos: null, enumerated_count: 0 })} />);
    expect(screen.queryByText('0 video mới')).not.toBeInTheDocument();
  });

  it('does NOT show the label once enumerated_count is above zero', () => {
    render(<ProgressBars job={job({ status: 'downloading', total_videos: 340, enumerated_count: 340 })} />);
    expect(screen.queryByText('0 video mới')).not.toBeInTheDocument();
  });
});
