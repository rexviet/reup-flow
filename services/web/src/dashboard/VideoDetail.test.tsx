import { render, screen, act, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as client from '../api/client';
import type { FolderExportView } from '../api/types';
import { VideoDetail } from './VideoDetail';
import { collection, COLLECTION_ID, detail } from './test-fixtures';

vi.mock('../api/client');

const renderedDetail = () =>
  detail({
    stage: 'rendered',
    render: { state: 'rendered', approved_revision: 'rev-9', attempts: 1, error: null, rendered_at: '2026-11-12T02:00:00Z', final_ready: true, srt_ready: true },
  });

beforeEach(() => {
  vi.mocked(client.getDashboardVideo).mockResolvedValue(detail());
  vi.mocked(client.getMediaLinks).mockResolvedValue([]);
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

describe('VideoDetail buttons follow the stage', () => {
  it('awaiting review: Mở Studio, Duyệt, Trả về; no render or publish buttons', async () => {
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Mở Studio' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Duyệt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Trả về' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Render lại' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Đánh dấu đã đăng' })).toBeNull();
    expect(screen.getByText(/studio mở trên máy chạy dashboard/i)).toBeInTheDocument();
  });

  it('run failed: Chạy tiếp từ bước lỗi and Chạy lại, nothing about review', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(detail({ stage: 'run_failed', review_status: null }));
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Chạy tiếp từ bước lỗi' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Chạy lại' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Duyệt' })).toBeNull();
  });

  it('returned: Mở Studio, Duyệt and Chạy lại (a new generation from the start), no Trả về', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(detail({ stage: 'returned' }));
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Mở Studio' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Duyệt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Chạy lại' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Trả về' })).toBeNull();
  });

  it('rendered: Render lại, a publish form, and MP4 and SRT downloads from signed links', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(renderedDetail());
    vi.mocked(client.getMediaLinks).mockResolvedValue([
      { kind: 'source', available: true, url: '/api/dashboard/media/aaa', expires_at: new Date(Date.now() + 600_000).toISOString() },
      { kind: 'final', available: true, url: '/api/dashboard/media/bbb', expires_at: new Date(Date.now() + 600_000).toISOString() },
      { kind: 'srt', available: true, url: '/api/dashboard/media/ccc', expires_at: new Date(Date.now() + 600_000).toISOString() },
    ]);
    render(<VideoDetail awemeId="7001" />);
    await flush();

    expect(client.getMediaLinks).toHaveBeenCalledWith('7001', 2, ['source', 'final', 'srt']);
    expect(screen.getByRole('button', { name: 'Render lại' })).toBeInTheDocument();
    expect(screen.getByLabelText('Video gốc')).toHaveAttribute('src', '/api/dashboard/media/aaa');
    expect(screen.getByLabelText('Bản cuối')).toHaveAttribute('src', '/api/dashboard/media/bbb');
    expect(screen.getByRole('link', { name: 'Tải MP4' })).toHaveAttribute('href', '/api/dashboard/media/bbb?download=1');
    expect(screen.getByRole('link', { name: 'Tải SRT' })).toHaveAttribute('href', '/api/dashboard/media/ccc?download=1');
  });

  it('asks for the final file of a legacy render that has no render row', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(detail({ stage: 'rendered', render: null }));
    vi.mocked(client.getMediaLinks).mockResolvedValue([
      { kind: 'source', available: true, url: '/api/dashboard/media/a' },
      { kind: 'final', available: true, url: '/api/dashboard/media/b', note: 'Bản cũ, không gắn generation.' },
    ]);
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(client.getMediaLinks).toHaveBeenCalledWith('7001', 2, ['source', 'final']);
    expect(screen.getByLabelText('Bản cuối')).toHaveAttribute('src', '/api/dashboard/media/b');
    expect(screen.getByText('Bản cũ, không gắn generation.')).toBeInTheDocument();
  });

  it('published: only the unmark button', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue({ ...renderedDetail(), stage: 'published' });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Bỏ đánh dấu đã đăng' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Render lại' })).toBeNull();
  });
});

describe('VideoDetail actions', () => {
  it('Duyệt sends the revision being shown and the current generation', async () => {
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7001'], skipped: [] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Duyệt' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'approve', aweme_ids: ['7001'], revision: 'rev-9', generation: 2 });
  });

  it('shows the revision conflict the server reports as a skip reason', async () => {
    vi.mocked(client.runAction).mockResolvedValue({ done: [], skipped: [{ aweme_id: '7001', reason: 'Revision đã thay đổi, tải lại trang.' }] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Duyệt' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(screen.getByTestId('action-notice')).toHaveTextContent('Revision đã thay đổi');
  });

  it('Duyệt is disabled while the project has unsaved changes', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(
      detail({ review_status: { generation: 2, revision: 'r', cueCount: 1, mismatchCount: 0, dirty: true, checkedAt: '2026-11-12T01:00:00Z' } }),
    );
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Duyệt' })).toBeDisabled();
  });

  it('shows the state of the newest Studio request', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(
      detail({ studio_request: { generation: 2, state: 'pending', error: null, requestedAt: '2026-11-12T01:00:00Z', handledAt: null } }),
    );
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByTestId('studio-status')).toHaveTextContent('đang chờ máy mở');
  });

  it('keeps the page up when a later poll says Postgres is down', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValueOnce(detail());
    render(<VideoDetail awemeId="7001" />);
    await flush();
    await userEvent.type(screen.getByLabelText('Ghi chú trả về'), 'giữ lại');
    vi.mocked(client.getDashboardVideo).mockResolvedValue({ video: null, sources: { postgres: 'down', temporal: 'ok' } });
    // The next real 3 s poll brings the degraded answer.
    expect(await screen.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent(/Postgres/);
    expect(screen.getByLabelText('Ghi chú trả về')).toHaveValue('giữ lại');
  }, 10000);

  it('Duyệt is disabled without a review revision', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(detail({ review_status: null }));
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Duyệt' })).toBeDisabled();
  });

  it('Trả về sends the note', async () => {
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7001'], skipped: [] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    await userEvent.type(screen.getByLabelText('Ghi chú trả về'), 'sửa câu 3');
    await userEvent.click(screen.getByRole('button', { name: 'Trả về' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'return', aweme_ids: ['7001'], generation: 2, note: 'sửa câu 3' });
  });

  it('Mở Studio posts open_studio and shows the pending-request reason', async () => {
    vi.mocked(client.runAction).mockResolvedValue({ done: [], skipped: [{ aweme_id: '7001', reason: 'Đã có yêu cầu mở Studio đang chờ.' }] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Mở Studio' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'open_studio', aweme_ids: ['7001'], generation: 2 });
    expect(screen.getByTestId('studio-status')).toHaveTextContent('Đã có yêu cầu mở Studio đang chờ.');
  });

  it('mark published needs a link and sends it', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(renderedDetail());
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7001'], skipped: [] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    const button = screen.getByRole('button', { name: 'Đánh dấu đã đăng' });
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Link bài đăng'), 'https://example.com/p/1');
    await userEvent.click(button);
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'mark_published', aweme_ids: ['7001'], generation: 2, post_url: 'https://example.com/p/1' });
  });
});

describe('VideoDetail facts', () => {
  it('shows mismatch count, cost, timeline, and action history; a missing Postgres still renders', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(
      detail({
        review_status: { generation: 2, revision: 'r', cueCount: 5, mismatchCount: 4, dirty: false, checkedAt: '2026-11-12T01:00:00Z' },
        timeline: [{ id: 's1', activity: 'translate_segments', agentId: 'a', state: 'done', startedAt: '2026-11-12T01:00:00Z', step: 'translate' }],
        dashboard_actions: [{ id: 1, action: 'open_studio', detail: {}, created_at: '2026-11-12T01:30:00Z' }],
        verdict: { ok: false },
      }),
    );
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByTestId('mismatch')).toHaveTextContent('4');
    expect(screen.getByTestId('llm-cost')).toHaveTextContent('$0.0123');
    expect(screen.getByText('translate_segments')).toBeInTheDocument();
    expect(screen.getByText('Lịch sử thao tác')).toBeInTheDocument();
  });

  it('says Postgres is down when the video is null', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue({ video: null, sources: { postgres: 'down', temporal: 'ok' } });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent(/Postgres/);
  });
});

const rendered = (generation: number, isCurrent: boolean) => ({
  generation,
  is_current: isCurrent,
  run: null,
  open_workflow: false,
  decision: null,
  render: { state: 'rendered', approved_revision: 'r', attempts: 1, error: null, rendered_at: '2026-11-12T02:00:00Z', final_ready: true, srt_ready: true },
  publication: null,
  review_status: null,
  llm_cost_usd: 0,
});
const OTHER_ID = '99999999-2222-4333-8444-555555555555';

describe('VideoDetail collection and save to folder', () => {
  it('shows the collection with its episode and moves the video to another collection', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(
      detail({ collection: { id: COLLECTION_ID, name: 'Phim A', kind: 'mix' }, video: { ...detail().video!, mix_episode: 4 } }),
    );
    vi.mocked(client.listCollections).mockResolvedValue({ items: [collection({ name: 'Phim A', kind: 'mix' }), collection({ id: OTHER_ID, name: 'Phim B' })] });
    vi.mocked(client.moveCollection).mockResolvedValue({ done: ['7001'], skipped: [] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByTestId('detail-collection')).toHaveTextContent('Phim A · Tập 4');
    await userEvent.selectOptions(screen.getByLabelText('Chuyển sang collection'), OTHER_ID);
    await userEvent.click(screen.getByRole('button', { name: 'Chuyển collection' }));
    await flush();
    expect(client.moveCollection).toHaveBeenCalledWith(['7001'], OTHER_ID);
  });

  it('no Lưu vào thư mục button before any final exists', async () => {
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.queryByRole('button', { name: 'Lưu vào thư mục' })).toBeNull();
  });

  it('the save button is off, with a hint, when the collection has no folder', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(detail({ stage: 'rendered', generations: [rendered(2, true)] }));
    vi.mocked(client.listCollections).mockResolvedValue({ items: [collection({ save_subdir: null })] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Lưu vào thư mục' })).toBeDisabled();
    expect(screen.getByTestId('folder-hint')).toHaveTextContent('chưa có thư mục lưu');
  });

  it('saves the generation picked among the rendered ones', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(
      detail({ stage: 'awaiting_review', current_gen: 3, generations: [{ ...rendered(3, true), render: null }, rendered(2, false), rendered(1, false)] }),
    );
    vi.mocked(client.listCollections).mockResolvedValue({ items: [collection({ save_subdir: 'phim-a' })] });
    vi.mocked(client.runAction).mockResolvedValue({ done: ['7001'], skipped: [] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    const picker = screen.getByLabelText('Generation để lưu');
    expect(within(picker).getAllByRole('option').map((o) => o.textContent)).toEqual(['g2', 'g1']);
    await userEvent.selectOptions(picker, '1');
    await userEvent.click(screen.getByRole('button', { name: 'Lưu vào thư mục' }));
    await flush();
    expect(client.runAction).toHaveBeenCalledWith({ action: 'save_to_folder', aweme_ids: ['7001'], generation: 1 });
  });

  it.each<[Pick<FolderExportView, 'state' | 'files' | 'error'>, string]>([
    [{ state: 'pending', files: null, error: null }, 'Đang chờ máy Mac lưu'],
    [{ state: 'exporting', files: null, error: null }, 'Đang lưu'],
    [{ state: 'done', files: ['phim-a/E01-7001.mp4', 'phim-a/E01-7001.srt'], error: null }, 'Đã lưu: phim-a/E01-7001.mp4, phim-a/E01-7001.srt'],
    [{ state: 'failed', files: null, error: 'Thư mục gốc chưa được gắn.' }, 'Lỗi: Thư mục gốc chưa được gắn.'],
  ])('shows the newest save request: %o', async (row, text) => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(
      detail({
        stage: 'rendered',
        generations: [rendered(2, true)],
        folder_export: {
          generation: 2,
          target_subdir: 'phim-a',
          file_stem: 'E01-7001',
          requested_at: '2026-11-14T01:00:00Z',
          handled_at: null,
          ...row,
        },
      }),
    );
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByTestId('folder-export-status')).toHaveTextContent(text);
  });

  it('a skipped save is reported in the notice, and the status line keeps showing the newest request', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(
      detail({
        stage: 'rendered',
        generations: [rendered(2, true)],
        folder_export: { generation: 2, state: 'pending', target_subdir: 'a', file_stem: 'E01-7001', files: null, error: null, requested_at: '2026-11-14T01:00:00Z', handled_at: null },
      }),
    );
    vi.mocked(client.runAction).mockResolvedValue({ done: [], skipped: [{ aweme_id: '7001', reason: 'Đã có yêu cầu lưu thư mục đang chờ hoặc đang lưu.' }] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Lưu vào thư mục' }));
    await flush();
    expect(screen.getByTestId('action-notice')).toHaveTextContent('Đã có yêu cầu lưu thư mục đang chờ');
    expect(screen.getByTestId('folder-export-status')).toHaveTextContent('Đang chờ máy Mac lưu');
  });

  it('leaves the save button on while the collections are not known yet; the server still checks', async () => {
    vi.mocked(client.getDashboardVideo).mockResolvedValue(detail({ stage: 'rendered', generations: [rendered(2, true)] }));
    vi.mocked(client.listCollections).mockResolvedValue({ items: [] });
    render(<VideoDetail awemeId="7001" />);
    await flush();
    expect(screen.getByRole('button', { name: 'Lưu vào thư mục' })).toBeEnabled();
    expect(screen.queryByTestId('folder-hint')).toBeNull();
  });
});
