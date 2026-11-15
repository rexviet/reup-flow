import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../api/client';
import type { AutoQueueStatus } from '../api/types';
import { AutoQueueSwitch } from './AutoQueueSwitch';

vi.mock('../api/client');

const status = (over: Partial<AutoQueueStatus> = {}): AutoQueueStatus => ({
  enabled: true,
  updated_at: '2026-11-14T06:00:00.000Z',
  in_flight: { aweme_id: '7692039666727472434', generation: 4, released_at: '2026-11-14T05:30:00.000Z' },
  held: 3,
  ...over,
});

beforeEach(() => {
  vi.resetAllMocks();
});

describe('AutoQueueSwitch', () => {
  it('shows the switch, the running video and how many are held', async () => {
    vi.mocked(client.getAutoQueue).mockResolvedValue(status());
    render(<AutoQueueSwitch />);
    const toggle = await screen.findByRole('switch');
    expect(toggle).toBeChecked();
    expect(screen.getByTestId('auto-queue')).toHaveTextContent('Đang chạy: 7692039666727472434');
    expect(screen.getByTestId('auto-queue')).toHaveTextContent('3 video đang giữ');
  });

  it('turning it off saves and shows the answer', async () => {
    vi.mocked(client.getAutoQueue).mockResolvedValue(status());
    vi.mocked(client.setAutoQueue).mockResolvedValue(status({ enabled: false }));
    render(<AutoQueueSwitch />);
    await userEvent.click(await screen.findByRole('switch'));
    expect(client.setAutoQueue).toHaveBeenCalledWith(false);
    await waitFor(() => expect(screen.getByRole('switch')).not.toBeChecked());
  });

  it('turning it on shows the video it started', async () => {
    vi.mocked(client.getAutoQueue).mockResolvedValue(status({ enabled: false, in_flight: null }));
    vi.mocked(client.setAutoQueue).mockResolvedValue(
      status({ in_flight: { aweme_id: '7000', generation: 1, released_at: '2026-11-14T06:01:00.000Z' }, held: 2 }),
    );
    render(<AutoQueueSwitch />);
    expect(await screen.findByText(/Không có video nào đang chạy/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('switch'));
    expect(client.setAutoQueue).toHaveBeenCalledWith(true);
    expect(await screen.findByText(/Đang chạy: 7000/)).toBeInTheDocument();
  });

  it('says so when saving fails and keeps the old state', async () => {
    vi.mocked(client.getAutoQueue).mockResolvedValue(status());
    vi.mocked(client.setAutoQueue).mockRejectedValue(new Error('HTTP 500'));
    render(<AutoQueueSwitch />);
    await userEvent.click(await screen.findByRole('switch'));
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 500');
    expect(screen.getByRole('switch')).toBeChecked();
  });
});
