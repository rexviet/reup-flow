import { render, screen, act, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as client from '../api/client';
import { DashboardPage } from './DashboardPage';
import { collection, emptyAttention, emptyChannels, health, item, list } from './test-fixtures';

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
  vi.mocked(client.listCollections).mockResolvedValue({ items: [] });
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
    vi.mocked(client.getDashboardVideos).mockResolvedValue(
      list([item({ aweme_id: '7005', stage: 'awaiting_review', revision: null })]),
    );
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7005'));
    expect(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Duyệt' })).toBeDisabled();
  });

  it('a failed run offers Chạy tiếp từ bước lỗi next to Chạy lại, and sends resume', async () => {
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([item({ aweme_id: '7007', stage: 'run_failed' })]));
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7007'], skipped: [] });
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7007'));
    const bar = screen.getByRole('toolbar');
    expect(within(bar).getByRole('button', { name: 'Chạy lại' })).toBeInTheDocument();
    await userEvent.click(within(bar).getByRole('button', { name: 'Chạy tiếp từ bước lỗi' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'resume', aweme_ids: ['7007'] });
  });

  it('offers only the actions that fit the selected stages, and Tải lại for a failed download', async () => {
    const download = item({ aweme_id: '7006', stage: 'download_failed', revision: null, current_gen: null });
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([download, held]));
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7006'], skipped: [] });
    render(<DashboardPage />);
    await flush();

    await userEvent.click(screen.getByLabelText('Chọn 7006'));
    const bar = screen.getByRole('toolbar');
    expect(within(bar).getByRole('button', { name: 'Tải lại' })).toBeEnabled();
    for (const name of ['Duyệt', 'Chạy lại', 'Render lại', 'Đánh dấu đã đăng', 'Đưa vào localization'])
      expect(within(bar).queryByRole('button', { name })).toBeNull();

    // Selecting a held video as well adds its own action; the download one stays.
    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    expect(within(bar).getByRole('button', { name: 'Đưa vào localization' })).toBeInTheDocument();
    expect(within(bar).getByRole('button', { name: 'Tải lại' })).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    await userEvent.click(within(bar).getByRole('button', { name: 'Tải lại' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'retry_download', aweme_ids: ['7006'] });
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

  it('a click on a collection lists its videos and marks it as the one shown', async () => {
    vi.mocked(client.listCollections).mockResolvedValue({ items: [collection({ id: 'c-a', name: 'Phim A' }), collection({ id: 'c-b', name: 'Phim B' })] });
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Xem video của Phim B' }));
    await flush();
    expect(client.getDashboardVideos).toHaveBeenLastCalledWith(expect.objectContaining({ collection: 'c-b', offset: 0 }));
    expect(screen.getByLabelText('Lọc theo collection')).toHaveValue('c-b');
    expect(screen.getByRole('button', { name: 'Xem video của Phim B' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Xem video của Phim A' })).toHaveAttribute('aria-pressed', 'false');
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
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([item({ aweme_id: '7008', stage: 'rendered' })]));
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7008'));
    await userEvent.click(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Đánh dấu đã đăng' }));
    await flush();
    expect(screen.getByTestId('action-notice')).toHaveTextContent('revision đã đổi');
  });
});

describe('DashboardPage rendered videos as pictures', () => {
  const rendered = [
    item({ aweme_id: '7101', stage: 'rendered', desc: 'Cảnh một' }),
    item({ aweme_id: '7102', stage: 'rendered', desc: 'Cảnh hai' }),
  ];

  async function openRendered() {
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([review, ...rendered]));
    render(<DashboardPage />);
    await flush();
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list(rendered));
    await userEvent.click(screen.getByTestId('chip-rendered'));
    await flush();
  }

  it('shows each rendered video as a card with its poster, and the other stages stay a table', async () => {
    vi.mocked(client.getMediaLinks).mockImplementation(async (awemeId) => [
      { kind: 'poster', available: true, url: `/api/dashboard/media/tok-${awemeId}`, expires_at: new Date(Date.now() + 600_000).toISOString() },
    ]);
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([review, ...rendered]));
    render(<DashboardPage />);
    await flush();
    expect(screen.queryByTestId('rendered-grid')).toBeNull();
    expect(screen.getAllByTestId('pipeline-row')).toHaveLength(3);

    vi.mocked(client.getDashboardVideos).mockResolvedValue(list(rendered));
    await userEvent.click(screen.getByTestId('chip-rendered'));
    await flush();

    expect(screen.queryByTestId('pipeline-row')).toBeNull();
    const cards = screen.getAllByTestId('rendered-card');
    expect(cards).toHaveLength(2);
    expect(client.getMediaLinks).toHaveBeenCalledWith('7101', 1, ['poster']);
    expect(within(cards[0]).getByRole('presentation', { hidden: true })).toHaveAttribute('src', '/api/dashboard/media/tok-7101');
    expect(within(cards[0]).getByRole('link', { name: 'Chi tiết 7101' })).toHaveAttribute('href', '#/video/7101');
    expect(cards[0]).toHaveTextContent('Cảnh một');
  });

  it('shows a placeholder for a video with no poster, and when the picture fails to load', async () => {
    vi.mocked(client.getMediaLinks).mockImplementation(async (awemeId) =>
      awemeId === '7101'
        ? [{ kind: 'poster', available: false }]
        : [{ kind: 'poster', available: true, url: '/api/dashboard/media/gone', expires_at: new Date(Date.now() + 600_000).toISOString() }],
    );
    await openRendered();
    const [first, second] = screen.getAllByTestId('rendered-card');
    expect(first).toHaveTextContent('Chưa có ảnh');

    const image = within(second).getByRole('presentation', { hidden: true });
    await act(async () => {
      image.dispatchEvent(new Event('error'));
    });
    expect(second).toHaveTextContent('Chưa có ảnh');
  });

  it('selects a card with its checkbox so the bulk bar works on pictures too', async () => {
    vi.mocked(client.getMediaLinks).mockResolvedValue([{ kind: 'poster', available: false }]);
    await openRendered();
    await userEvent.click(screen.getByLabelText('Chọn 7102'));
    expect(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Đánh dấu đã đăng' })).toBeInTheDocument();
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
    expect(client.createJob).toHaveBeenCalledWith('https://www.douyin.com/user/abc', true, undefined);

    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/video/7100');
    await userEvent.click(within(form).getByLabelText('Localize ngay'));
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await flush();
    expect(client.createVideo).toHaveBeenCalledWith('https://www.douyin.com/video/7100', true, undefined);

    // Without the box ticked the channel's own switch decides: the flag is left out, not sent as false.
    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/video/7101');
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await flush();
    expect(client.createVideo).toHaveBeenLastCalledWith('https://www.douyin.com/video/7101', undefined, undefined);
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

const MIX_ID = '99999999-2222-4333-8444-555555555555';
const withFolder = collection({ id: MIX_ID, name: 'Phim A', kind: 'mix', save_subdir: 'phim-a' });
const noFolder = collection();

describe('DashboardPage collections', () => {
  beforeEach(() => {
    vi.mocked(client.listCollections).mockResolvedValue({ items: [withFolder, noFolder] });
  });

  it('filters the list by collection', async () => {
    render(<DashboardPage />);
    await flush();
    await userEvent.selectOptions(screen.getByLabelText('Lọc theo collection'), MIX_ID);
    await flush();
    expect(client.getDashboardVideos).toHaveBeenLastCalledWith(expect.objectContaining({ collection: MIX_ID, offset: 0 }));
    await userEvent.selectOptions(screen.getByLabelText('Lọc theo collection'), '');
    await flush();
    expect(vi.mocked(client.getDashboardVideos).mock.lastCall?.[0]).not.toHaveProperty('collection');
  });

  it('shows each video\'s collection and episode', async () => {
    vi.mocked(client.getDashboardVideos).mockResolvedValue(
      list([item({ aweme_id: '7008', collection: { id: MIX_ID, name: 'Phim A', kind: 'mix' }, mix_episode: 3 })]),
    );
    render(<DashboardPage />);
    await flush();
    const cell = within(screen.getByTestId('pipeline-row')).getByTestId('collection-cell');
    expect(cell).toHaveTextContent('Phim A');
    expect(cell).toHaveTextContent('Tập 3');
  });

  it('creates a job in the picked collection, and with none when left on the default', async () => {
    vi.mocked(client.createJob).mockResolvedValue({ job_id: 'j1' });
    render(<DashboardPage />);
    await flush();
    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/user/x');
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await flush();
    expect(client.createJob).toHaveBeenLastCalledWith('https://www.douyin.com/user/x', undefined, undefined);

    await userEvent.selectOptions(screen.getByLabelText('Collection'), MIX_ID);
    await userEvent.type(screen.getByLabelText(/url profile/i), 'https://www.douyin.com/user/x');
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await flush();
    expect(client.createJob).toHaveBeenLastCalledWith('https://www.douyin.com/user/x', undefined, MIX_ID);
  });

  it('moves the selected videos to another collection', async () => {
    vi.mocked(client.moveCollection).mockResolvedValue({ done: ['7001', '7002'], skipped: [] });
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7001'));
    await userEvent.click(screen.getByLabelText('Chọn 7002'));
    const bar = screen.getByRole('toolbar');
    const move = within(bar).getByRole('button', { name: 'Chuyển collection' });
    expect(move).toBeDisabled();
    await userEvent.selectOptions(within(bar).getByLabelText('Collection đích'), MIX_ID);
    await userEvent.click(move);
    await flush();
    expect(client.moveCollection).toHaveBeenCalledWith(['7001', '7002'], MIX_ID);
    expect(screen.getByTestId('move-notice')).toHaveTextContent('Đã chuyển 2 video sang «Phim A»');
  });

  it('save to folder: offered for finished videos, off when their collection has no folder, sent without a generation', async () => {
    const inFolder = item({ aweme_id: '7011', stage: 'rendered', collection: { id: MIX_ID, name: 'Phim A', kind: 'mix' } });
    const homeless = item({ aweme_id: '7012', stage: 'published' });
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([inFolder, homeless, review]));
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7011'], skipped: [] });
    render(<DashboardPage />);
    await flush();

    await userEvent.click(screen.getByLabelText('Chọn 7001'));
    const bar = screen.getByRole('toolbar');
    expect(within(bar).queryByRole('button', { name: 'Lưu vào thư mục' })).toBeNull();

    await userEvent.click(screen.getByLabelText('Chọn 7001'));
    await userEvent.click(screen.getByLabelText('Chọn 7012'));
    const off = within(screen.getByRole('toolbar')).getByRole('button', { name: 'Lưu vào thư mục' });
    expect(off).toBeDisabled();
    expect(off).toHaveAttribute('title', expect.stringContaining('chưa có thư mục lưu'));

    await userEvent.click(screen.getByLabelText('Chọn 7012'));
    await userEvent.click(screen.getByLabelText('Chọn 7011'));
    const on = within(screen.getByRole('toolbar')).getByRole('button', { name: 'Lưu vào thư mục' });
    expect(on).toBeEnabled();
    await userEvent.click(on);
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'save_to_folder', aweme_ids: ['7011'] });
  });

  it('lists the collections in their own panel', async () => {
    render(<DashboardPage />);
    await flush();
    const panel = screen.getByRole('region', { name: 'Collections' });
    expect(within(panel).getAllByTestId('collection-row')).toHaveLength(2);
  });

  it('bulk save stays on with a visible hint when only some selected videos lack a folder', async () => {
    const inFolder = item({ aweme_id: '7011', stage: 'rendered', collection: { id: MIX_ID, name: 'Phim A', kind: 'mix' } });
    const homeless = item({ aweme_id: '7012', stage: 'published' });
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([inFolder, homeless]));
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7011'));
    await userEvent.click(screen.getByLabelText('Chọn 7012'));
    const bar = screen.getByRole('toolbar');
    expect(within(bar).getByRole('button', { name: 'Lưu vào thư mục' })).toBeEnabled();
    expect(within(bar).getByTestId('bulk-save-hint')).toHaveTextContent('1 video sẽ bị bỏ qua');
  });

  it('bulk save stays on while the collections are not known yet', async () => {
    vi.mocked(client.listCollections).mockResolvedValue({ items: [] });
    vi.mocked(client.getDashboardVideos).mockResolvedValue(list([item({ aweme_id: '7012', stage: 'published' })]));
    render(<DashboardPage />);
    await flush();
    await userEvent.click(screen.getByLabelText('Chọn 7012'));
    expect(within(screen.getByRole('toolbar')).getByRole('button', { name: 'Lưu vào thư mục' })).toBeEnabled();
  });

  it('says so when the collections cannot be loaded instead of loading forever', async () => {
    vi.mocked(client.listCollections).mockRejectedValue(new Error('mạng hỏng'));
    render(<DashboardPage />);
    await flush();
    expect(within(screen.getByRole('region', { name: 'Collections' })).getByText(/Không tải được collection: mạng hỏng/)).toBeInTheDocument();
  });
});
