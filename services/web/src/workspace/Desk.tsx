import type { AgentState, WorkspaceAgent } from '../api/types';
import { shortId, videoColor } from './videoColor';
import { STATION, type DeskSpec } from './workspace.layout';

/** What a desk shows. `unknown` is "we cannot tell", never a claim that it is idle. */
export type DeskDisplayState = AgentState | 'unknown';

export function stateLabel(state: DeskDisplayState, attempt?: number): string {
  switch (state) {
    case 'idle':
      return 'rảnh';
    case 'working':
      return 'đang làm';
    case 'retrying':
      return attempt ? `thử lại lần ${attempt}` : 'thử lại';
    case 'failed':
      return 'lỗi';
    case 'unknown':
      return 'không rõ';
  }
}

const SCREEN: Record<DeskDisplayState, string> = {
  idle: '#374151',
  working: '#34d399',
  retrying: '#fbbf24',
  failed: '#ef4444',
  unknown: '#9ca3af',
};
const TEXT: Record<DeskDisplayState, string> = {
  idle: '#6b7280',
  working: '#047857',
  retrying: '#b45309',
  failed: '#b91c1c',
  unknown: '#9ca3af',
};
const SHIRTS = ['#6366f1', '#ec4899', '#f59e0b', '#14b8a6', '#8b5cf6', '#ef4444', '#3b82f6', '#84cc16'];

function shirtFor(id: string): string {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return SHIRTS[h % SHIRTS.length];
}

const MAX_BUBBLE_CHARS = 38;

function SvgBubble({ text, awemeId }: { text: string; awemeId?: string }) {
  const shown = text.length > MAX_BUBBLE_CHARS ? `${text.slice(0, MAX_BUBBLE_CHARS - 1)}…` : text;
  const tag = awemeId ? `…${shortId(awemeId)} ` : '';
  const w = Math.max((tag.length + shown.length) * 6 + 16, 40);
  return (
    <g role="status" className="motion-safe:animate-fade-in" transform={`translate(${STATION.w / 2 - w / 2}, -26)`}>
      <title>{text}</title>
      <rect width={w} height={20} rx={6} fill="#111827" />
      <path d={`M${w / 2 - 5},20 l5,6 l5,-6 z`} fill="#111827" />
      <text x={8} y={14} fontSize={11} fill="#fff">
        {awemeId && <tspan fill={videoColor(awemeId)} fontWeight={700}>{tag}</tspan>}
        {shown}
      </text>
    </g>
  );
}

export interface DeskProps {
  spec: DeskSpec;
  agent?: WorkspaceAgent;
  /** Forces the display state, used when the source the state comes from is down. */
  forcedState?: DeskDisplayState;
  bubble?: { text: string; awemeId?: string };
  selected?: boolean;
  completed?: boolean;
  waiting?: boolean;
  onSelect?: (awemeId: string, generation: number) => void;
}

/**
 * One station seen from above: a chair, a person, a desk with a monitor. The
 * monitor colour and the person's motion follow the agent's state, and the state
 * is also written under the name so nothing depends on colour alone.
 */
export function Desk({ spec, agent, forcedState, bubble, selected, completed, waiting, onSelect }: DeskProps) {
  const state: DeskDisplayState = forcedState ?? agent?.state ?? 'idle';
  const awemeId = forcedState ? undefined : agent?.awemeId;
  const generation = agent?.generation;
  const canOpen = awemeId !== undefined && generation !== undefined && onSelect !== undefined;
  const active = state === 'working' || state === 'retrying';
  const status = forcedState ? stateLabel(state) : completed ? 'xong lượt gần nhất' : waiting ? 'chưa có bước' : stateLabel(state, agent?.attempt);
  const shirt = shirtFor(spec.id);

  const personMotion =
    state === 'working' ? 'motion-safe:animate-bob' : state === 'retrying' ? 'motion-safe:animate-shake' : '';
  const interaction = canOpen
    ? {
        role: 'button',
        tabIndex: 0,
        'aria-label': `${spec.label}: ${status}`,
        onClick: () => onSelect(awemeId, generation),
        onKeyDown: (e: { key: string }) => {
          if (e.key === 'Enter' || e.key === ' ') onSelect(awemeId, generation);
        },
        style: { cursor: 'pointer' },
      }
    : {};

  return (
    <g transform={`translate(${spec.x}, ${spec.y})`} data-desk={spec.id} data-state={state} opacity={waiting && !forcedState ? 0.45 : 1} {...interaction}>
      {selected && <rect x={-4} y={-4} width={STATION.w + 8} height={STATION.h + 8} rx={10} fill="none" stroke="#3b82f6" strokeWidth={2} />}
      {/* chair */}
      <circle cx={65} cy={32} r={17} fill="#d1d5db" />
      {/* person, seen from above */}
      <g className={personMotion} opacity={state === 'idle' || state === 'unknown' ? 0.55 : 1}>
        <ellipse cx={65} cy={36} rx={16} ry={10} fill={shirt} />
        <circle cx={65} cy={26} r={10} fill="#f5d0a9" />
        <path d="M55,25 a10,10 0 0 1 20,0 z" fill="#3f2d1d" />
      </g>
      {state === 'failed' && (
        <g>
          <circle cx={92} cy={14} r={9} fill="#ef4444" />
          <text x={92} y={18} textAnchor="middle" fontSize={13} fontWeight={700} fill="#fff">!</text>
        </g>
      )}
      {/* desk with monitor */}
      <rect x={8} y={50} width={114} height={30} rx={5} fill="#c8a27a" stroke="#a37c52" />
      <rect x={44} y={54} width={42} height={17} rx={2} fill={completed && !forcedState ? '#bbf7d0' : SCREEN[state]} className={active ? 'motion-safe:animate-glow' : ''} />
      {awemeId && (
        <g>
          <rect x={96} y={56} width={16} height={20} rx={1.5} fill="#fff" stroke={videoColor(awemeId)} strokeWidth={2} />
          <line x1={99} y1={62} x2={109} y2={62} stroke={videoColor(awemeId)} />
          <line x1={99} y1={67} x2={109} y2={67} stroke={videoColor(awemeId)} />
        </g>
      )}
      <text x={65} y={94} textAnchor="middle" fontSize={12} fontWeight={600} fill="#111827">{spec.label}</text>
      <text x={65} y={108} textAnchor="middle" fontSize={11} fill={completed && !forcedState ? '#047857' : TEXT[state]}>{status}</text>
      {awemeId && !completed && <text x={65} y={121} textAnchor="middle" fontSize={10} fontWeight={700} fill={videoColor(awemeId)}>…{awemeId.slice(-8)}</text>}
      {bubble && <SvgBubble text={bubble.text} awemeId={bubble.awemeId} />}
    </g>
  );
}
