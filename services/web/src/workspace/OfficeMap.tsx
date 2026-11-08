import type { Bubbles } from './useEventFeed';
import type { RunDetail, WorkspaceAgent, WorkspaceEvent, WorkspaceSnapshot } from '../api/types';
import { Desk, type DeskDisplayState } from './Desk';
import { HandoffLine } from './HandoffLine';
import { LINE_MS } from './useEventFeed';
import { deskCenter, deskLabel, OFFICE_SIZE, officeZones, PORTRAIT_OFFICE_SIZE, unknownDesk } from './workspace.layout';

export interface OfficeMapProps {
  snapshot: WorkspaceSnapshot | null;
  bubbles: Record<string, Bubbles>;
  handoffs: WorkspaceEvent[];
  selected?: { awemeId: string; generation: number } | null;
  detail?: RunDetail | null;
  fullscreen?: boolean;
  onSelect: (awemeId: string, generation: number) => void;
}

/**
 * The office seen from above. Desk states come from Temporal; when it cannot be
 * read (or nothing has loaded yet) a desk says "không rõ" instead of "rảnh",
 * because an idle desk would be a claim.
 */
export function OfficeMap({ snapshot, bubbles, handoffs, selected, detail, fullscreen = false, onSelect }: OfficeMapProps) {
  const [portrait, setPortrait] = useState(false);
  useEffect(() => {
    if (!fullscreen || !window.matchMedia) return;
    const query = window.matchMedia('(max-aspect-ratio: 1/1)');
    const update = () => setPortrait(query.matches);
    update();
    query.addEventListener?.('change', update);
    return () => query.removeEventListener?.('change', update);
  }, [fullscreen]);
  const size = portrait ? PORTRAIT_OFFICE_SIZE : OFFICE_SIZE;
  const forced: DeskDisplayState | undefined =
    !snapshot || snapshot.sources.temporal === 'down' || detail?.sources.temporal === 'down' || (selected && !detail) ? 'unknown' : undefined;
  const agents = new Map<string, WorkspaceAgent>((snapshot?.agents ?? []).map((a) => [a.id, a]));
  const steps = new Map<string, RunDetail['steps'][number]>();
  for (const step of detail?.steps ?? []) {
    const held = steps.get(step.agentId);
    const active = step.state === 'running' || step.state === 'retrying';
    const heldActive = held?.state === 'running' || held?.state === 'retrying';
    if (heldActive && (!active || (held.state === 'retrying' && step.state === 'running'))) continue;
    steps.set(step.agentId, step);
  }
  if (selected && detail) {
    for (const [id, agent] of agents) {
      const step = steps.get(id);
      agents.set(id, { ...agent, state: step?.state === 'running' ? 'working' : step?.state === 'retrying' ? 'retrying' : step?.state === 'failed' ? 'failed' : 'idle',
        awemeId: step ? selected.awemeId : undefined, generation: step ? selected.generation : undefined, attempt: step?.attempt });
    }
  }

  const deskProps = (id: string) => ({
    agent: agents.get(id),
    forcedState: forced,
    bubble: selected && bubbles[id]?.awemeId !== selected.awemeId ? undefined : bubbles[id],
    selected: selected != null && agents.get(id)?.awemeId === selected.awemeId && agents.get(id)?.generation === selected.generation && (agents.get(id)?.state === 'working' || agents.get(id)?.state === 'retrying'),
    onSelect: selected ? undefined : onSelect,
    completed: !!detail && steps.get(id)?.state === 'done',
    waiting: !!detail && !steps.has(id),
  });

  const lines = handoffs.filter((h) => !selected || (h.awemeId === selected.awemeId && h.generation === selected.generation)).sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).flatMap((h) => {
    const from = h.agentId ? deskCenter(h.agentId, portrait) : null;
    const to = h.toAgentId ? deskCenter(h.toAgentId, portrait) : null;
    return from && to
      ? [{ id: h.id, from, to, fromLabel: deskLabel(h.agentId as string), toLabel: deskLabel(h.toAgentId as string), awemeId: h.awemeId, durationMs: LINE_MS }]
      : [];
  }).slice(0, 1);

  return (
    <div className={fullscreen ? 'h-full w-full' : 'flex flex-col gap-4'}>
      {!fullscreen && <div><h2 className="text-sm font-semibold text-gray-900">Văn phòng{selected ? ` · video …${selected.awemeId.slice(-8)}` : ''}</h2><p className="text-xs text-gray-600">Bàn sáng đang làm · xong lượt gần nhất · bàn mờ chưa có hoạt động. Mũi tên chỉ hiện bàn giao mới nhất của video đang chọn.</p></div>}
      {snapshot?.sources.temporal === 'down' && (
        <p role="alert" className={`${fullscreen ? 'absolute bottom-14 left-3 right-3 z-10 ' : ''}rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800`}>
          Không đọc được Temporal, nên chưa biết bàn nào đang làm. Các bàn hiển thị “không rõ”.
        </p>
      )}
      <svg
        viewBox={`0 0 ${size.w} ${size.h}`}
        className={fullscreen ? 'h-full w-full bg-stone-100' : 'w-full rounded-lg border border-gray-300 bg-stone-100'}
        role="group"
        aria-label="Văn phòng"
      >
        <defs>
          <pattern id="floor" width="40" height="40" patternUnits="userSpaceOnUse">
            <rect width="40" height="40" fill="#e7e5e4" />
            <path d="M40,0 H0 V40" fill="none" stroke="#d6d3d1" strokeWidth="1" />
          </pattern>
        </defs>
        {officeZones(portrait).map((zone) => (
          <g key={zone.zone} role="region" aria-label={zone.title}>
            <rect x={zone.room.x} y={zone.room.y} width={zone.room.w} height={zone.room.h} rx={12} fill="url(#floor)" stroke="#78716c" strokeWidth={3} />
            <text x={zone.room.x + 14} y={zone.room.y + zone.room.h - 6} fontSize={12} fontWeight={700} fill="#44403c">
              {zone.title}
            </text>
            {zone.desks.map((spec) => (
              <Desk key={spec.id} spec={spec} {...deskProps(spec.id)} />
            ))}
          </g>
        ))}
        {agents.has('unknown') && (
          <g role="region" aria-label="Chưa rõ">
            <Desk spec={unknownDesk(portrait)} {...deskProps('unknown')} />
          </g>
        )}
        {lines.map((l) => (
          <HandoffLine key={l.id} {...l} />
        ))}
      </svg>
    </div>
  );
}
import { useEffect, useState } from 'react';
