import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { HealthBar } from './HealthBar';
import type { HostWorkerView } from '../api/types';
import { health } from './test-fixtures';

describe('HealthBar', () => {
  it('shows one badge per concern, coloured by state, with the reason as tooltip', () => {
    const report = health({
      cookie: { status: 'down', reason: 'Chưa có cookie Douyin; job tải sẽ bị chặn.' },
      disk: { status: 'degraded', reason: 'Đĩa còn 12 GB.' },
      host_worker: { status: 'unknown', reason: 'Không đọc được.' },
    });
    render(<HealthBar health={report} error={null} />);

    for (const id of ['cookie', 'voicestudio', 'stt', 'tts', 'ingest', 'host_worker', 'temporal', 'minio', 'disk']) {
      expect(screen.getByTestId(`health-${id}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId('health-cookie')).toHaveAttribute('data-state', 'down');
    expect(screen.getByTestId('health-cookie')).toHaveAttribute('title', 'Chưa có cookie Douyin; job tải sẽ bị chặn.');
    expect(screen.getByTestId('health-cookie').className).toMatch(/status-error|health-down/);
    expect(screen.getByTestId('health-disk')).toHaveAttribute('data-state', 'degraded');
    expect(screen.getByTestId('health-disk').className).toMatch(/status-warning|health-degraded/);
    expect(screen.getByTestId('health-host_worker').className).toMatch(/surface-container-low/);
    expect(screen.getByTestId('health-temporal').className).toMatch(/surface-container-low/);
    // status is also written out, not carried by colour alone
    expect(screen.getByTestId('health-cookie')).toHaveTextContent('Hỏng');
    expect(screen.getByTestId('health-disk')).toHaveTextContent('Suy giảm');
    expect(screen.getByTestId('health-host_worker')).toHaveTextContent('Không rõ');
    expect(screen.getByTestId('health-temporal')).toHaveTextContent('Ổn');
  });

  it('keeps the last report and says the connection dropped when a later poll fails', () => {
    render(<HealthBar health={health()} error={new Error('x')} />);
    expect(screen.getByTestId('health-cookie')).toBeInTheDocument();
    expect(screen.getByText(/mất kết nối/i)).toBeInTheDocument();
  });

  it('says why when there is no report yet', () => {
    render(<HealthBar health={null} error={new Error('boom')} />);
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });

  describe('one line per machine', () => {
    // generatedAt is 2026-11-12T01:00:00Z, 08:00 in Saigon.
    const worker = (over: Partial<HostWorkerView>): HostWorkerView => ({
      name: 'review-worker',
      alive: true,
      seen_at: '2026-11-12T00:59:50.000Z',
      code_version: 'def5678',
      protocol: 2,
      current: null,
      outdated: false,
      outdated_reason: null,
      latest_code_version: 'def5678',
      ...over,
    });

    it('shows an idle live machine and one that is rendering, with their versions and no warning', () => {
      const report = health({
        host_workers: [
          worker({ name: 'render-worker-win', current: { aweme_id: '7001', generation: 1, progress: '35% Capturing frames' } }),
          worker({}),
        ],
      });
      render(<HealthBar health={report} error={null} />);
      expect(screen.getByTestId('host-review-worker')).toHaveTextContent('Máy Mac· rảnh· bản def5678');
      expect(screen.getByTestId('host-render-worker-win')).toHaveTextContent('Máy Windows render· đang render 7001 35% Capturing frames· bản def5678');
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('says since when a silent machine lost its connection', () => {
      render(<HealthBar health={health({ host_workers: [worker({ name: 'render-worker-win', alive: false, seen_at: '2026-11-12T00:41:00.000Z' })] })} error={null} />);
      const line = screen.getByTestId('host-render-worker-win');
      expect(line).toHaveAttribute('data-alive', 'false');
      expect(line).toHaveTextContent('mất kết nối từ 07:41');
      expect(line).not.toHaveTextContent('rảnh');
    });

    it('warns about a machine whose code does not report its version', () => {
      const report = health({
        host_workers: [worker({ name: 'render-worker-win', code_version: null, protocol: null, outdated: true, outdated_reason: 'no_version' })],
      });
      render(<HealthBar health={report} error={null} />);
      expect(screen.getByTestId('host-render-worker-win')).toHaveTextContent('bản cũ (chưa báo phiên bản)');
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Máy Windows render đang chạy bản cũ (chưa báo phiên bản). Trên máy đó, bấm đúp file cap-nhat-may-render.bat trong thư mục reup-flow.',
      );
    });

    it('warns about a machine on another version than the Mac, naming both', () => {
      const report = health({
        host_workers: [worker({ name: 'render-worker-win', code_version: 'abc1234', outdated: true, outdated_reason: 'version' }), worker({})],
      });
      render(<HealthBar health={report} error={null} />);
      expect(screen.getByTestId('host-render-worker-win')).toHaveAttribute('data-outdated', 'true');
      expect(screen.getAllByRole('alert')).toHaveLength(1);
      expect(screen.getByRole('alert')).toHaveTextContent('Máy Windows render đang chạy bản cũ (abc1234, bản mới def5678). Trên máy đó, bấm đúp file cap-nhat-may-render.bat');
    });
  });
});
