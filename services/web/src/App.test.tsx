import { render, screen, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import App, { routeFromHash } from './App';
import * as client from './api/client';
import { detail, emptyAttention, emptyChannels, health, item, list } from './dashboard/test-fixtures';

vi.mock('./api/client');

beforeEach(() => {
  vi.mocked(client.getCookieStatus).mockResolvedValue({ present: true, age_seconds: 60, last_ok_at: null });
  vi.mocked(client.listJobs).mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
  vi.mocked(client.getHealth).mockResolvedValue(health());
  vi.mocked(client.getDashboardVideos).mockResolvedValue(list([item()]));
  vi.mocked(client.getAttention).mockResolvedValue(emptyAttention);
  vi.mocked(client.getChannels).mockResolvedValue(emptyChannels);
  vi.mocked(client.getDashboardVideo).mockResolvedValue(detail());
  vi.mocked(client.getMediaLinks).mockResolvedValue([]);
});

afterEach(() => {
  vi.resetAllMocks();
  window.location.hash = '';
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('routeFromHash', () => {
  it('opens the detail page only for a numeric aweme id', () => {
    expect(routeFromHash('#/video/7001')).toEqual({ page: 'video', awemeId: '7001' });
    expect(routeFromHash('#/video/abc')).toEqual({ page: 'dashboard' });
    expect(routeFromHash('#workspace')).toEqual({ page: 'dashboard' });
    expect(routeFromHash('')).toEqual({ page: 'dashboard' });
  });
});

describe('App', () => {
  it('is a single dashboard page: health, attention, pipeline and the download area, with no old tabs', async () => {
    render(<App />);
    await flush();
    expect(screen.getByTestId('health-bar')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /cần xử lý/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /pipeline/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/url profile/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Workspace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review' })).toBeNull();
    expect(client.getDashboardVideo).not.toHaveBeenCalled();
  });

  it('shows the detail page for #/video/<aweme> and goes back on hashchange', async () => {
    window.location.hash = '#/video/7001';
    render(<App />);
    await flush();
    expect(client.getDashboardVideo).toHaveBeenCalledWith('7001');
    expect(screen.getByRole('heading', { name: '7001' })).toBeInTheDocument();

    await act(async () => {
      window.location.hash = '#/';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    await flush();
    expect(screen.getByRole('heading', { name: /pipeline/i })).toBeInTheDocument();
  });
});
