import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { detail } from './fixtures';
import { RunProgress } from './RunProgress';

describe('RunProgress', () => {
  it('keeps repeated visits to TTS separate and identifies the current retry', () => {
    render(<RunProgress detail={detail({ steps: [
      { id: '1', activity: 'synthesize_segments', agentId: 'tts', state: 'done', startedAt: '2026-11-01T12:00:00Z', endedAt: '2026-11-01T12:00:01Z' },
      { id: '2', activity: 'agent__dubber_code__model_request', agentId: 'dubber', state: 'done', startedAt: '2026-11-01T12:00:02Z', endedAt: '2026-11-01T12:00:03Z' },
      { id: '3', activity: 'synthesize_segments', agentId: 'tts', state: 'retrying', attempt: 2, startedAt: '2026-11-01T12:00:04Z' },
    ] })} />);
    expect(screen.getByText('2/3 bước đã xong · thứ tự được lên lịch')).toBeInTheDocument();
    expect(screen.getByText('TTS · Đang thử lại lần 2')).toBeInTheDocument();
    expect(within(screen.getByRole('list')).getAllByText('Tạo giọng đọc')).toHaveLength(2);
    expect(screen.getByText('Thử lại bước hiện tại')).toBeInTheDocument();
  });

  it('marks unavailable live data as unknown instead of claiming the last step is running', () => {
    render(<RunProgress stale detail={detail({ steps: [
      { id: '1', activity: 'transcribe', agentId: 'stt', state: 'running', startedAt: '2026-11-01T12:00:00Z' },
    ] })} />);
    expect(screen.getByText('Trạng thái chưa rõ')).toBeInTheDocument();
    expect(screen.getByText('Chờ kết nối dữ liệu mới')).toBeInTheDocument();
    expect(screen.queryByText('STT · Đang chạy')).toBeNull();
  });

  it('shows a terminal human-review outcome without predicting another step', () => {
    render(<RunProgress detail={detail({ status: 'needs_human', outcome: 'needs_human' })} />);
    expect(screen.getByText('Cần người xem')).toBeInTheDocument();
    expect(screen.getByText('Không còn bước tiếp theo trong lượt chạy này.')).toBeInTheDocument();
    expect(screen.queryByText('Đang chờ workflow quyết định')).toBeNull();
  });
});

it('shows ready_for_review as terminal manual delivery', () => {
  render(<RunProgress detail={detail({ status: 'ready_for_review', outcome: 'ready_for_review' })} />);
  expect(screen.getByText('Sẵn sàng review')).toBeInTheDocument();
  expect(screen.getByText('Không còn bước tiếp theo trong lượt chạy này.')).toBeInTheDocument();
  expect(screen.queryByText('Đã xuất bản')).toBeNull();
});
