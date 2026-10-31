import type { Bubbles } from './useEventFeed';
import type { WorkspaceAgent, WorkspaceEvent, WorkspaceSnapshot } from '../api/types';
import { Desk, type DeskDisplayState } from './Desk';
import { GateArea } from './GateArea';
import { HandoffLine } from './HandoffLine';
import { LINE_MS } from './useEventFeed';
import { deskCenter, deskLabel, GATE_COLUMNS, GATE_ROOM, OFFICE_SIZE, UNKNOWN_DESK, ZONES } from './workspace.layout';

export interface OfficeMapProps {
  snapshot: WorkspaceSnapshot | null;
  bubbles: Record<string, Bubbles>;
  handoffs: WorkspaceEvent[];
  selected?: { awemeId: string; generation: number } | null;
  onSelect: (awemeId: string, generation: number) => void;
}

/**
 * The office seen from above. Desk states come from Temporal; when it cannot be
 * read (or nothing has loaded yet) a desk says "không rõ" instead of "rảnh",
 * because an idle desk would be a claim.
 */
export function OfficeMap({ snapshot, bubbles, handoffs, selected, onSelect }: OfficeMapProps) {
  const forced: DeskDisplayState | undefined =
    !snapshot || snapshot.sources.temporal === 'down' ? 'unknown' : undefined;
  const agents = new Map<string, WorkspaceAgent>((snapshot?.agents ?? []).map((a) => [a.id, a]));
  const runs = snapshot?.runs ?? [];

  const deskProps = (id: string) => ({
    agent: agents.get(id),
    forcedState: forced,
    bubble: bubbles[id],
    selected: selected != null && agents.get(id)?.awemeId === selected.awemeId,
    onSelect,
  });

  const lines = handoffs.flatMap((h) => {
    const from = h.agentId ? deskCenter(h.agentId) : null;
    const to = h.toAgentId ? deskCenter(h.toAgentId) : null;
    return from && to
      ? [{ id: h.id, from, to, fromLabel: deskLabel(h.agentId as string), toLabel: deskLabel(h.toAgentId as string), awemeId: h.awemeId, durationMs: LINE_MS }]
      : [];
  });

  const count = (outcome: string) => runs.filter((r) => r.outcome === outcome).length;
  const others = runs.filter((r) => r.outcome === undefined).length;

  return (
    <div className="flex flex-col gap-4">
      {snapshot?.sources.temporal === 'down' && (
        <p role="alert" className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800">
          Không đọc được Temporal, nên chưa biết bàn nào đang làm. Các bàn hiển thị “không rõ”.
        </p>
      )}
      <svg
        viewBox={`0 0 ${OFFICE_SIZE.w} ${OFFICE_SIZE.h}`}
        className="w-full rounded-lg border border-gray-300 bg-stone-100"
        role="group"
        aria-label="Văn phòng"
      >
        <defs>
          <pattern id="floor" width="40" height="40" patternUnits="userSpaceOnUse">
            <rect width="40" height="40" fill="#e7e5e4" />
            <path d="M40,0 H0 V40" fill="none" stroke="#d6d3d1" strokeWidth="1" />
          </pattern>
        </defs>
        {ZONES.map((zone) => (
          <g key={zone.zone} role="region" aria-label={zone.title}>
            <rect {...zone.room} rx={12} fill="url(#floor)" stroke="#78716c" strokeWidth={3} />
            <text x={zone.room.x + 14} y={zone.room.y + zone.room.h - 10} fontSize={14} fontWeight={700} fill="#44403c">
              {zone.title}
            </text>
            {zone.desks.map((spec) => (
              <Desk key={spec.id} spec={spec} {...deskProps(spec.id)} />
            ))}
          </g>
        ))}
        {agents.has('unknown') && (
          <g role="region" aria-label="Chưa rõ">
            <Desk spec={UNKNOWN_DESK} {...deskProps('unknown')} />
          </g>
        )}
        {/* The gate: a row of bins, one per decision. Counts only; the list below opens a video. */}
        <g aria-hidden="true">
          <rect {...GATE_ROOM} rx={12} fill="#fafaf9" stroke="#78716c" strokeWidth={3} />
          <text x={GATE_ROOM.x + 14} y={GATE_ROOM.y + 22} fontSize={14} fontWeight={700} fill="#44403c">Cổng</text>
          {[...GATE_COLUMNS.map((c) => ({ label: c.label, n: count(c.outcome) })), { label: 'Khác', n: others }].map((bin, i) => (
            <g key={bin.label} transform={`translate(${40 + i * 188}, ${GATE_ROOM.y + 32})`}>
              <rect width={170} height={86} rx={8} fill="#e7e5e4" stroke="#a8a29e" strokeWidth={2} />
              <rect x={10} y={10} width={150} height={10} rx={3} fill="#a8a29e" />
              <text x={85} y={52} textAnchor="middle" fontSize={26} fontWeight={700} fill="#292524">{bin.n}</text>
              <text x={85} y={76} textAnchor="middle" fontSize={11} fill="#57534e">{bin.label}</text>
            </g>
          ))}
        </g>
        {lines.map((l) => (
          <HandoffLine key={l.id} {...l} />
        ))}
      </svg>
      <GateArea snapshot={snapshot} bubble={bubbles.gate} selected={selected} onSelect={onSelect} />
    </div>
  );
}
