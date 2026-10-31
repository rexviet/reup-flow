import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { OfficeMap } from './OfficeMap';
import { ALL_DESKS } from './workspace.layout';
import { agent, event, snapshot } from './fixtures';

const noop = { bubbles: {}, handoffs: [], onSelect: vi.fn() };

describe('OfficeMap', () => {
  it('draws all four zones and every desk, even with an empty snapshot', () => {
    render(<OfficeMap snapshot={snapshot()} {...noop} />);
    for (const name of ['Media', 'Voice', 'LLM', 'Cổng']) {
      expect(screen.getByRole('region', { name })).toBeInTheDocument();
    }
    for (const desk of ALL_DESKS) {
      expect(document.querySelector(`[data-desk="${desk.id}"]`)).not.toBeNull();
    }
    expect(screen.queryByText('đang làm')).toBeNull();
    expect(screen.getAllByText('rảnh')).toHaveLength(ALL_DESKS.length);
  });

  it('shows only the working desk as working', () => {
    const s = snapshot({ agents: [agent({ id: 'stt', zone: 'voice', state: 'working', awemeId: '123456789', generation: 1 })] });
    render(<OfficeMap snapshot={s} {...noop} />);
    expect(screen.getAllByText('đang làm')).toHaveLength(1);
    expect(document.querySelector('[data-desk="stt"]')).toHaveAttribute('data-state', 'working');
  });

  it('with Temporal down draws no desk as working or idle, and says why', () => {
    const s = snapshot({
      sources: { temporal: 'down', postgres: 'ok' },
      agents: [agent({ id: 'stt', zone: 'voice', state: 'working', awemeId: '1', generation: 1 })],
    });
    render(<OfficeMap snapshot={s} {...noop} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Không đọc được Temporal');
    expect(screen.queryByText('đang làm')).toBeNull();
    expect(screen.queryByText('rảnh')).toBeNull();
  });

  it('before anything has loaded draws every desk as unknown', () => {
    render(<OfficeMap snapshot={null} {...noop} />);
    expect(screen.queryByText('đang làm')).toBeNull();
    expect(screen.getAllByText('không rõ')).toHaveLength(ALL_DESKS.length);
  });

  it('files runs by gate outcome, with the other runs under "Khác" by phase', () => {
    const s = snapshot({
      runs: [
        { awemeId: '1000000001', generation: 1, phase: 'skipped_ad', outcome: 'skipped_ad', llmCostUsd: 0 },
        { awemeId: '1000000002', generation: 1, phase: 'skipped_no_video', outcome: 'skipped_no_video', llmCostUsd: 0 },
        { awemeId: '1000000003', generation: 1, phase: 'published_original', outcome: 'published_original', llmCostUsd: 0 },
        { awemeId: '1000000004', generation: 1, phase: 'needs_human', outcome: 'needs_human', llmCostUsd: 0 },
        { awemeId: '1000000005', generation: 2, phase: 'published', llmCostUsd: 0.1 },
        { awemeId: '1000000006', generation: 1, phase: 'dubber', llmCostUsd: 0 },
      ],
    });
    render(<OfficeMap snapshot={s} {...noop} />);
    const column = (outcome: string) => within(document.querySelector(`[data-outcome="${outcome}"]`) as HTMLElement);
    expect(column('skipped_ad').getByRole('button')).toHaveAccessibleName(/1000000001/);
    expect(column('skipped_no_video').getByRole('button')).toHaveAccessibleName(/1000000002/);
    expect(column('published_original').getByRole('button')).toHaveAccessibleName(/1000000003/);
    expect(column('needs_human').getByRole('button')).toHaveAccessibleName(/1000000004/);
    const others = column('other').getAllByRole('button');
    expect(others.map((b) => b.getAttribute('aria-label'))).toEqual([
      expect.stringContaining('đã đăng'),
      expect.stringContaining('Dubber'),
    ]);
  });

  it('draws no handoff line, and no bubble, when there are no events', () => {
    render(<OfficeMap snapshot={snapshot()} {...noop} />);
    expect(document.querySelector('line')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('draws a line for a handoff event and nothing for another kind', () => {
    const handoff = event({ id: 'h1', kind: 'handoff', agentId: 'media', toAgentId: 'stt', awemeId: '7665948504426075877', detail: 'media handed over to stt' });
    const { rerender } = render(<OfficeMap snapshot={snapshot()} {...noop} handoffs={[handoff]} />);
    expect(screen.getByTestId('handoff-h1')).toBeInTheDocument();
    // The arrow says who handed over to whom, and for which video.
    expect(screen.getByText('Media → STT · …5877')).toBeInTheDocument();
    rerender(<OfficeMap snapshot={snapshot()} {...noop} handoffs={[]} />);
    expect(screen.queryByTestId('handoff-h1')).toBeNull();
  });

  it('draws exactly one line per handoff, and none for a handoff whose desks are not on the map', () => {
    const real = event({ id: 'h1', kind: 'handoff', agentId: 'media', toAgentId: 'stt' });
    const stray = event({ id: 'h2', kind: 'handoff', agentId: 'media', toAgentId: 'no-such-desk' });
    render(<OfficeMap snapshot={snapshot()} {...noop} handoffs={[real, stray]} />);
    expect(document.querySelectorAll('line')).toHaveLength(1);
    expect(screen.queryByTestId('handoff-h2')).toBeNull();
  });

  it('puts a bubble on the desk it is for and a gate bubble in the gate area', () => {
    render(<OfficeMap snapshot={snapshot()} {...noop} bubbles={{ dubber: { text: 'dubber done in 3.0s' }, gate: { text: 'skipped: advertisement' } }} />);
    expect(within(document.querySelector('[data-desk="dubber"]') as HTMLElement).getByRole('status')).toHaveTextContent('dubber done in 3.0s');
    expect(within(screen.getByRole('region', { name: 'Cổng' })).getByRole('status')).toHaveTextContent('skipped: advertisement');
  });

  it('lists an activity the server could not place under "Chưa rõ"', () => {
    const s = snapshot({ agents: [agent({ id: 'unknown', zone: 'unknown', state: 'working', awemeId: '5', generation: 1 })] });
    render(<OfficeMap snapshot={s} {...noop} />);
    expect(screen.getByRole('region', { name: 'Chưa rõ' })).toBeInTheDocument();
  });
});
