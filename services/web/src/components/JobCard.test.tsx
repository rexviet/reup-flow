import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { JobCard } from './JobCard';
import type { JobResponse } from '../api/types';

function job(overrides: Partial<JobResponse>): JobResponse {
  return {
    job_id: 'j1',
    sec_user_id: 'sec1',
    status: 'downloading',
    total_videos: 340,
    enumerated_count: 340,
    downloaded_count: 127,
    failed_count: 0,
    max_cursor: 0,
    error: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('JobCard', () => {
  it('shows the profile id and status', () => {
    render(<JobCard job={job({})} onCancel={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByText('sec1')).toBeInTheDocument();
    expect(screen.getByText('downloading')).toBeInTheDocument();
  });

  it('shows a Cancel button for a cancellable status and calls onCancel with the job id', async () => {
    const onCancel = vi.fn();
    render(<JobCard job={job({ status: 'downloading' })} onCancel={onCancel} onRetry={vi.fn()} />);
    const btn = screen.getByRole('button', { name: /hủy/i });
    await userEvent.click(btn);
    expect(onCancel).toHaveBeenCalledWith('j1');
  });

  it('does not show a Cancel button for a terminal status', () => {
    render(<JobCard job={job({ status: 'completed' })} onCancel={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /hủy/i })).not.toBeInTheDocument();
  });

  it('shows a Retry button only for blocked/failed and calls onRetry with the job id', async () => {
    const onRetry = vi.fn();
    render(<JobCard job={job({ status: 'blocked', error: 'stopped after 3 consecutive 400s' })} onCancel={vi.fn()} onRetry={onRetry} />);
    const btn = screen.getByRole('button', { name: /thử lại/i });
    await userEvent.click(btn);
    expect(onRetry).toHaveBeenCalledWith('j1');
  });

  it('does not show a Retry button for a downloading job', () => {
    render(<JobCard job={job({ status: 'downloading' })} onCancel={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /thử lại/i })).not.toBeInTheDocument();
  });

  it('shows the error message when present (blocked jobs carry the F4 park reason)', () => {
    render(
      <JobCard
        job={job({ status: 'blocked', error: 'the cookie is being refused' })}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText(/the cookie is being refused/)).toBeInTheDocument();
  });
});
