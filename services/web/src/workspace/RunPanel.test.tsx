import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as client from '../api/client';
import { RunDetailView, RunPanel } from './RunPanel';
import { detail } from './fixtures';
import type { GateOutcome } from '../api/types';

vi.mock('../api/client');
afterEach(() => vi.resetAllMocks());

describe('RunDetailView', () => {
  it.each<[GateOutcome, string]>([
    ['skipped_ad', 'Quảng cáo'],
    ['skipped_no_video', 'Không có tiếng hoặc hình'],
    ['published_original', 'Đăng bản gốc'],
    ['ready_for_review', 'Sẵn sàng review'],
    ['needs_human', 'Cần người xem'],
  ])('labels the gate outcome %s', (outcome, label) => {
    render(<RunDetailView detail={detail({ status: outcome, outcome })} />);
    expect(screen.getByTestId('run-result')).toHaveTextContent(label);
  });

  it('labels a published run', () => {
    render(<RunDetailView detail={detail({ status: 'published' })} />);
    expect(screen.getByTestId('run-result')).toHaveTextContent('đã đăng');
  });

  it('shows a zero cost as $0.0000 instead of hiding it', () => {
    render(<RunDetailView detail={detail({ llmCostUsd: 0, costByStage: [] })} />);
    expect(screen.getByTestId('run-total-cost')).toHaveTextContent('$0.0000');
  });

  it('shows the per-stage rows and the total', () => {
    render(
      <RunDetailView
        detail={detail({
          llmCostUsd: 0.2505,
          costByStage: [
            { stage: 'agent__dubber_code__model_request', calls: 4, costUsd: 0.25 },
            { stage: 'classify_video', calls: 1, costUsd: 0.0005 },
          ],
        })}
      />,
    );
    expect(screen.getByText('agent__dubber_code__model_request')).toBeInTheDocument();
    expect(screen.getByText('$0.2500')).toBeInTheDocument();
    expect(screen.getByTestId('run-total-cost')).toHaveTextContent('$0.2505');
  });

  it('always says the GPU time is an estimate, not an invoice', () => {
    render(<RunDetailView detail={detail({ gpuSecondsEstimate: { stt: 0, tts: 0 } })} />);
    expect(screen.getByText(/chưa phải hoá đơn/)).toBeInTheDocument();
    expect(screen.getByText(/STT 0\.0 giây/)).toBeInTheDocument();
  });

  it('lists phases with their state', () => {
    render(
      <RunDetailView
        detail={detail({
          phases: [
            { agentId: 'media', state: 'done', startedAt: '2026-10-31T06:00:00Z', endedAt: '2026-10-31T06:01:00Z', activities: 1 },
            { agentId: 'tts', state: 'retrying', startedAt: '2026-10-31T06:01:00Z', activities: 3 },
          ],
        })}
      />,
    );
    expect(screen.getByText('Media')).toBeInTheDocument();
    expect(screen.getByText('đang thử lại · 3 bước')).toBeInTheDocument();
  });

  it('warns when a source was down, since the numbers may be missing', () => {
    render(<RunDetailView detail={detail({ sources: { temporal: 'down', postgres: 'ok' } })} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Không đọc được Temporal');
  });
});

describe('RunPanel', () => {
  it('loads the detail of the run it was opened for', async () => {
    vi.mocked(client.getRunDetail).mockResolvedValue(detail({ llmCostUsd: 0.5 }));
    render(<RunPanel awemeId="7665948504426075877" generation={7} onClose={vi.fn()} />);
    expect(await screen.findByTestId('run-total-cost')).toHaveTextContent('$0.5000');
    expect(client.getRunDetail).toHaveBeenCalledWith('7665948504426075877', 7);
  });

  it('says so when the detail cannot be loaded', async () => {
    vi.mocked(client.getRunDetail).mockRejectedValue(new Error('run 1/1 not found'));
    render(<RunPanel awemeId="1" generation={1} onClose={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('run 1/1 not found');
  });
});
