import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Desk } from './Desk';
import { agent } from './fixtures';

const spec = { id: 'dubber', label: 'Dubber', x: 0, y: 0 };

describe('Desk', () => {
  it.each([
    ['idle', 'rảnh'],
    ['working', 'đang làm'],
    ['failed', 'lỗi'],
  ] as const)('says %s in words, not only in colour', (state, text) => {
    render(<Desk spec={spec} agent={agent({ id: 'dubber', state })} />);
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it('says which attempt a retrying desk is on', () => {
    render(<Desk spec={spec} agent={agent({ id: 'dubber', state: 'retrying', attempt: 3 })} />);
    expect(screen.getByText('thử lại lần 3')).toBeInTheDocument();
  });

  it('shows the video it is working on and opens it on click', async () => {
    const onSelect = vi.fn();
    render(
      <Desk spec={spec} agent={agent({ id: 'dubber', state: 'working', awemeId: '7665948504426075877', generation: 7 })} onSelect={onSelect} />,
    );
    expect(screen.getByText('…26075877')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button'));
    expect(onSelect).toHaveBeenCalledWith('7665948504426075877', 7);
  });

  it('is idle, and not clickable, with no data for it', () => {
    render(<Desk spec={spec} />);
    expect(screen.getByText('rảnh')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('never claims "đang làm" when the state is forced to unknown', () => {
    render(<Desk spec={spec} agent={agent({ id: 'dubber', state: 'working', awemeId: '1', generation: 1 })} forcedState="unknown" />);
    expect(screen.queryByText('đang làm')).toBeNull();
    expect(screen.getByText('không rõ')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('shows a bubble only when given one', () => {
    const { rerender } = render(<Desk spec={spec} />);
    expect(screen.queryByRole('status')).toBeNull();
    rerender(<Desk spec={spec} bubble={{ text: 'anthropic/x, 10 tokens, $0.0001', awemeId: '7665948504426075877' }} />);
    expect(screen.getByRole('status')).toHaveTextContent('…5877 anthropic/x, 10 tokens, $0.0001');
  });
});
