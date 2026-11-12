import { render, screen, act, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as client from '../api/client';
import { DashboardPage } from './DashboardPage';
import { emptyAttention, emptyChannels, health, item, list } from './test-fixtures';

vi.mock('../api/client');

const held = item({ aweme_id: '7002', stage: 'held', revision: null, current_gen: null });
const failed = item({ aweme_id: '7003', stage: 'run_failed', revision: null });
const review = item({ aweme_id: '7001', stage: 'awaiting_review' });

beforeEach(() => {
  vi.mocked(client.getCookieStatus).mockResolvedValue({ present: true, age_seconds: 60, last_ok_at: null });
  vi.mocked(client.listJobs).mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
  vi.mocked(client.getHealth).mockResolvedValue(health());
  vi.mocked(client.getDashboardVideos).mockResolvedValue(list([review, held, failed]));
  vi.mocked(client.getAttention).mockResolvedValue(emptyAttention);
  vi.mocked(client.getChannels).mockResolvedValue(emptyChannels);
});

afterEach(() => {
  vi.resetAllMocks();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('DashboardPage pipeline', () => {
  it('keeps Duyệt disabled for a single selected video that has no review revision', async () => {
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    expect(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Duyệt' })).toBeDisabled();
  });

  it('marks a guessed stage with a question mark and Escape closes the confirm dialog', async () => {
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([item({ aweme_id: '7010', stage: 'held', unknown: true, revision: null })]));
    render(<DashboardPage />);
    await flush();
    expect(screen.getByText('Đang giữ ?')).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText('Chọn 7010'));
    await userEvent.click(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Đưa vào localization' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(client.runAction).not.toHaveBeenCalled();
  });

  it('has an all-stages chip with the total, shows duration and a text marker for rows needing attention', async () => {
    const flagged = item({
      aweme_id: '7004',
      duration_ms: 95_000,
      attention: [{ code: 'mismatch', reason: 'Có cue lệch.', action: 'Mở Studio.' }],
    });
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([review, held, flagged]));
    render(<DashboardPage />);
    await flush();

    expect(screen.getByTestId('chip-all')).toHaveTextContent('Tất cả 3');
    expect(screen.getByTestId('chip-all')).toHaveAttribute('aria-pressed', 'true');
    const row = screen.getAllByTestId('pipeline-row').find((r) => r.getAttribute('data-aweme-id') === '7004')!;
    expect(within(row).getByTitle('Thời lượng video')).toHaveTextContent('1:35');
    expect(within(row).getByText('Có cue lệch.')).toBeInTheDocument();
    expect(within(row).getByRole('link', { name: 'Chi tiết 7004' })).toHaveAttribute('href', '#/video/7004');

    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([held]));
    await userEvent.click(screen.getByTestId('chip-held'));
    await flush();
    await userEvent.click(screen.getByTestId('chip-all'));
    expect(screen.getByTestId('chip-held')).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows a chip per stage with its count and filters the query when a chip is clicked', async () => {
    render(<DashboardPage />);
    await flush();
    expect(screen.getByTestId('chip-held')).toHaveTextContent('Đang giữ 1');
    expect(screen.getAllByTestId('pipeline-row')).toHaveLength(3);

    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([held]));
    await userEvent.click(screen.getByTestId('chip-held'));
    await flush();

    expect(client.getDashboardVideos).toHaveBeenLastCalledWith(expect.objectContaining({ stage: 'held' }));
    expect(screen.getAllByTestId('pipeline-row')).toHaveLength(1);
    expect(screen.getByTestId('chip-held')).toHaveAttribute('aria-pressed', 'true');

    await userEvent.click(screen.getByTestId('chip-held'));
    await flush();
    expect(vi.mocked(client.getDashboardVideos).mock.lastCall?.[0]).not.toHaveProperty('stage');
  });

  it('sends the search text after a pause', async () => {
    render(<DashboardPage />);
    await flush();
    await userEvent.type(screen.getByLabelText('Tìm kiếm'), 'mèo');
    await waitFor(() => expect(client.getDashboardVideos).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'mèo', offset: 0 })));
  });

  it('shows stage with the localizing sub-step and a warning when a source is down', async () => {
    vi.mocked(client.getDashboardVideos).mockResolvedValue(
      list([item({ aweme_id: '7005', stage: 'localizing', step: 'dub' })], { sources: { postgres: 'ok', temporal: 'down' } }),
    );
    render(<DashboardPage />);
    await flush();
    expect(screen.getByText('Đang localize (lồng tiếng)')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/Temporal/);
  });
});

describe('DashboardPage bulk actions', () => {
  it('asks with the video count, then posts the selected aweme_ids and shows done and skipped reasons', async () => {
    vi.mocked(client.runAction).mockResolvedValue({
      done: ['7002'],
      skipped: [{ aweme_id: '7003', reason: 'Đang có workflow chạy.' }],
    });
    render(<DashboardPage />);
    await flush();

    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    await userEvent.click(screen.getByLabelText('Chọn 7003'));
    const bar = screen.getByRole('toolbar');
    expect(bar).toHaveTextContent('Đã chọn 2');
    await userEvent.click(within(bar).getByRole('button', { name: 'Chạy lại' }));

    const dialog = screen.getByRole('alertdialog');
    expect(dialog).toHaveTextContent('Chạy lại cho 2 video?');
    expect(client.runAction).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Xác nhận' }));
    await flush();

    expect(client.runAction).toHaveBeenCalledWith({ action: 'rerun', aweme_ids: ['7002', '7003'] });
    const notice = screen.getByTestId('action-notice');
    expect(notice).toHaveTextContent('1 thành công, 1 bị bỏ qua');
    expect(notice).toHaveTextContent('Đang có workflow chạy.');
    // the skipped one stays selected, the done one leaves
    expect(screen.getByLabelText('Chọn 7003')).toBeChecked();
    expect(screen.getByLabelText('Chọn 7002')).not.toBeChecked();
  });

  it('does not post when the confirmation is cancelled', async () => {
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    await userEvent.click(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Đưa vào localization' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Hủy' }));
    await flush();
    expect(client.runAction).not.toHaveBeenCalled();
  });

  it('keeps Duyệt off for several videos and sends the shown revision for one', async () => {
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7001'], skipped: [] });
    render(<DashboardPage />);
    await flush();

    await userEvent.click(screen.getByLabelText('Chọn 7001'));
    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    expect(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Duyệt' })).toBeDisabled();

    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    const approve = within(screen.getByRole('toolbar')).getByRole('button', { name: 'Duyệt' });
    expect(approve).toBeEnabled();
    await userEvent.click(approve);
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({
      action: 'approve',
      aweme_ids: ['7001'],
      revision: 'rev-1',
      generation: 1,
    });
  });

  it('asks separately before approving with mismatches, then sends confirm_mismatch', async () => {
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([item({ mismatch_count: 3 })]));
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7001'], skipped: [] });
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7001'));
    await userEvent.click(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Duyệt' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));

    const second = await screen.findByRole('alertdialog');
    expect(second).toHaveTextContent('3 chỗ lời đọc lệch');
    expect(client.runAction).not.toHaveBeenCalled();
    await userEvent.click(within(second).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith(expect.objectContaining({ action: 'approve', confirm_mismatch: true, revision: 'rev-1' }));
  });

  it('shows the server error when the action fails', async () => {
    vi.mocked(client.runAction).mockRejectedValue(new Error('revision đã đổi'));
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    await userEvent.click(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Đánh dấu đã đăng' }));
    await flush();
    expect(screen.getByTestId('action-notice')).toHaveTextContent('revision đã đổi');
  });
});

describe('DashboardPage attention and channels', () => {
  it('lists blocked jobs and videos with their reason and a suggested action', async () => {
    vi.mocked(client.getAttention).mockResolvedValue({
      jobs: [{ id: 'j1', sec_user_id: 'sec1', channel: 'Kênh A', status: 'blocked', error: null, updated_at: '2026-11-12T00:00:00Z', reason: 'Job tải bị chặn.', action: 'Đẩy cookie mới.' }],
      items: [item({ aweme_id: '7003', stage: 'run_failed', attention: [{ code: 'run_failed', reason: 'Lần chạy localization thất bại.', action: 'Chạy lại video.' }] })],
      sources: { postgres: 'ok', temporal: 'ok' },
    });
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7003'], skipped: [] });
    vi.mocked(client.retryJob).mockResolvedValue({ job: {} as never, videos_reset: 0 });
    render(<DashboardPage />);
    await flush();

    expect(screen.getByTestId('attention-job')).toHaveTextContent('Job tải bị chặn.');
    const row = screen.getByTestId('attention-item');
    expect(row).toHaveTextContent('Lần chạy localization thất bại.');

    await userEvent.click(within(screen.getByTestId('attention-job')).getByRole('button', { name: 'Thử lại job' }));
    expect(client.retryJob).toHaveBeenCalledWith('j1');

    await userEvent.click(within(row).getByRole('button', { name: 'Chạy lại' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'rerun', aweme_ids: ['7003'] });
  });

  it('flips a channel auto-localize switch and submits a channel link with the switch', async () => {
    vi.mocked(client.getChannels).mockResolvedValue({
      items: [{ sec_user_id: 'sec1', nickname: 'Kênh A', auto_localize: false, last_synced_at: null, latest_job: null, total: 3, counts_by_stage: {} as never }],
      sources: { postgres: 'ok', temporal: 'ok' },
    });
    vi.mocked(client.setAutoLocalize).mockResolvedValue();
    vi.mocked(client.createJob).mockResolvedValue({ job_id: 'n' });
    vi.mocked(client.createVideo).mockResolvedValue({ aweme_id: '7100', status: 'pending' });
    render(<DashboardPage />);
    await flush();

    const row = screen.getByTestId('channel-row');
    await userEvent.click(within(row).getByLabelText('Tự localize'));
    await flush();
    expect(client.setAutoLocalize).toHaveBeenCalledWith('sec1', true);

    const form = screen.getByLabelText(/url profile/i).closest('form')!;
    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/user/abc');
    await userEvent.click(within(form).getByLabelText('Tự localize'));
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await flush();
    expect(client.createJob).toHaveBeenCalledWith('https://www.douyin.com/user/abc', true);

    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/video/7100');
    await userEvent.click(within(form).getByLabelText('Localize ngay'));
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await flush();
    expect(client.createVideo).toHaveBeenCalledWith('https://www.douyin.com/video/7100', true);

    // Without the box ticked the channel's own switch decides: the flag is left out, not sent as false.
    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/video/7101');
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await flush();
    expect(client.createVideo).toHaveBeenLastCalledWith('https://www.douyin.com/video/7101', undefined);
  });

  it('names the revision and mismatch count in the approve confirmation and blocks a double job retry', async () => {
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([item({ revision: 'rev-77', mismatch_count: 0 })]));
    vi.mocked(client.getAttention).mockResolvedValue({
      jobs: [{ id: 'j1', sec_user_id: 's', channel: null, status: 'blocked', error: null, updated_at: '2026-11-12T00:00:00Z', reason: 'r', action: 'a' }],
      items: [],
      sources: { postgres: 'ok', temporal: 'ok' },
    });
    let finish: () => void = () => {};
    vi.mocked(client.retryJob).mockReturnValue(new Promise((resolve) => { finish = () => resolve({ job: {} as never, videos_reset: 0 }); }));
    render(<DashboardPage />);
    await flush();

    await userEvent.click(screen.getByLabelText('Chọn 7001'));
    await userEvent.click(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Duyệt' }));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('revision rev-77, 0 chỗ lệch');
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Hủy' }));

    const retry = screen.getByRole('button', { name: 'Thử lại job' });
    await userEvent.click(retry);
    expect(retry).toBeDisabled();
    await act(async () => finish());
  });
});
