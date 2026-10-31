import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TopBar } from './TopBar';
import { snapshot } from './fixtures';

describe('TopBar', () => {
  it('says "Dữ liệu thật" only when both sources are ok and the data is current', () => {
    render(<TopBar snapshot={snapshot()} staleSeconds={0} loading={false} />);
    expect(screen.getByText('Dữ liệu thật')).toBeInTheDocument();
    expect(screen.getByText('Temporal: ổn')).toBeInTheDocument();
    expect(screen.getByText('Postgres: ổn')).toBeInTheDocument();
  });

  it.each([['temporal', 'Temporal: lỗi'], ['postgres', 'Postgres: lỗi']] as const)(
    'drops "Dữ liệu thật" and names %s when it is down',
    (source, text) => {
      const s = snapshot({ sources: { temporal: 'ok', postgres: 'ok', [source]: 'down' } });
      render(<TopBar snapshot={s} staleSeconds={0} loading={false} />);
      expect(screen.queryByText('Dữ liệu thật')).toBeNull();
      expect(screen.getByText(text)).toBeInTheDocument();
    },
  );

  it('says how old the data is when polls are failing, and drops "Dữ liệu thật"', () => {
    render(<TopBar snapshot={snapshot()} staleSeconds={12} loading={false} />);
    expect(screen.getByText('cũ 12 giây')).toBeInTheDocument();
    expect(screen.queryByText('Dữ liệu thật')).toBeNull();
  });

  it('says it is loading, then that it failed, when there is no snapshot', () => {
    const { rerender } = render(<TopBar snapshot={null} staleSeconds={0} loading />);
    expect(screen.getByText(/Đang tải/)).toBeInTheDocument();
    rerender(<TopBar snapshot={null} staleSeconds={5} loading={false} />);
    expect(screen.getByText(/Không tải được dữ liệu workspace/)).toBeInTheDocument();
    expect(screen.queryByText('Dữ liệu thật')).toBeNull();
  });
});
