import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { HealthBar } from './HealthBar';
import { health } from './test-fixtures';

describe('HealthBar', () => {
  it('shows one badge per concern, coloured by state, with the reason as tooltip', () => {
    const report = health({
      cookie: { status: 'down', reason: 'Chưa có cookie Douyin; job tải sẽ bị chặn.' },
      disk: { status: 'degraded', reason: 'Đĩa còn 12 GB.' },
      host_worker: { status: 'unknown', reason: 'Không đọc được.' },
    });
    render(<HealthBar health={report} error={null} />);

    for (const id of ['cookie', 'voicestudio', 'stt', 'tts', 'host_worker', 'temporal', 'minio', 'disk']) {
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
});
