import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import App from './App';
import * as client from './api/client';
import type { JobResponse } from './api/types';

vi.mock('./api/client');

function job(overrides: Partial<JobResponse>): JobResponse {
  return {
    job_id: 'j1',
    sec_user_id: 'sec1',
    status: 'downloading',
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

beforeEach(() => {
  vi.mocked(client.getCookieStatus).mockResolvedValue({ present: true, age_seconds: 60, last_ok_at: null });
  vi.mocked(client.listJobs).mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
});

afterEach(() => {
  vi.resetAllMocks();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('App (real timers -- interactions)', () => {
  it('renders the cookie badge (always visible, not in a menu) and the job form', async () => {
    render(<App />);
    await flush();
    expect(screen.getByTestId('cookie-badge')).toBeInTheDocument();
    expect(screen.getByLabelText(/url profile/i)).toBeInTheDocument();
  });

  it('submitting a URL creates a job and it shows up in the list on the next poll', async () => {
    vi.mocked(client.createJob).mockResolvedValue({ job_id: 'new-job' });
    render(<App />);
    await flush();

    vi.mocked(client.listJobs).mockResolvedValue({
      items: [job({ job_id: 'new-job' })],
      total: 1,
      limit: 20,
      offset: 0,
    });

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/user/xyz');
    await user.click(screen.getByRole('button', { name: /download all/i }));
    expect(client.createJob).toHaveBeenCalledWith('https://www.douyin.com/user/xyz');

    // Real 1s poll interval (§8.1) -- wait past one real tick rather than
    // fighting fake timers + userEvent's own internal timers together.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1100));
    });
    expect(screen.getByTestId('job-card')).toHaveAttribute('data-job-id', 'new-job');
  }, 10000);

  it('clicking Cancel on a job calls cancelJob with its id', async () => {
    vi.mocked(client.listJobs).mockResolvedValue({
      items: [job({ job_id: 'j1', status: 'downloading' })],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(client.cancelJob).mockResolvedValue(job({ job_id: 'j1', status: 'cancelled' }));

    render(<App />);
    await flush();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /hủy/i }));
    expect(client.cancelJob).toHaveBeenCalledWith('j1');
  });
});

describe('App tabs', () => {
  afterEach(() => {
    window.location.hash = '';
  });

  it('opens on Jobs and does not poll the workspace until its tab is opened', async () => {
    vi.mocked(client.getWorkspaceSnapshot).mockResolvedValue({
      generatedAt: new Date().toISOString(),
      sources: { temporal: 'ok', postgres: 'ok' },
      agents: [],
      events: [],
      runs: [],
    });
    render(<App />);
    await flush();
    expect(screen.getByRole('button', { name: 'Jobs' })).toHaveAttribute('aria-current', 'page');
    expect(client.getWorkspaceSnapshot).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Workspace' }));
    await flush();
    expect(client.getWorkspaceSnapshot).toHaveBeenCalled();
    expect(screen.getByRole('region', { name: 'Cổng' })).toBeInTheDocument();
    expect(screen.queryByTestId('cookie-badge')).toBeNull();
    expect(window.location.hash).toBe('#workspace');
  });

  it('opens on the Workspace tab when the address says so', async () => {
    window.location.hash = '#workspace';
    vi.mocked(client.getWorkspaceSnapshot).mockRejectedValue(new Error('down'));
    render(<App />);
    await flush();
    expect(screen.getByRole('button', { name: 'Workspace' })).toHaveAttribute('aria-current', 'page');
  });
});

describe('App (fake timers -- polling cadence)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('polls jobs and cookie status every 1s', async () => {
    render(<App />);
    await flush();
    expect(client.listJobs).toHaveBeenCalledTimes(1);
    expect(client.getCookieStatus).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await Promise.resolve();
    });
    expect(client.listJobs).toHaveBeenCalledTimes(2);
    expect(client.getCookieStatus).toHaveBeenCalledTimes(2);
  });
});
