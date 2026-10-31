import { videoColor } from './videoColor';

export interface Point {
  x: number;
  y: number;
}

function motionAllowed(): boolean {
  return typeof window.matchMedia !== 'function' || !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export interface HandoffLineProps {
  id: string;
  from: Point;
  to: Point;
  fromLabel: string;
  toLabel: string;
  awemeId?: string;
  durationMs: number;
}

/**
 * One handoff: an arrow from the desk that finished to the desk that took over, in
 * the colour of the video, with a label saying who handed what to whom. A document
 * walks along it once, and the whole thing fades out before the next event.
 */
export function HandoffLine({ id, from, to, fromLabel, toLabel, awemeId, durationMs }: HandoffLineProps) {
  const color = videoColor(awemeId);
  const markerId = `arrow-${id.replace(/[^A-Za-z0-9]/g, '')}`;
  // Stop short of the desk so the arrowhead is visible and does not sit on the person.
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.hypot(dx, dy) || 1;
  const end = { x: to.x - (dx / len) * 34, y: to.y - (dy / len) * 34 };
  const start = { x: from.x + (dx / len) * 34, y: from.y + (dy / len) * 34 };
  const mid = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
  const label = `${fromLabel} → ${toLabel}${awemeId ? ` · …${awemeId.slice(-4)}` : ''}`;
  const labelW = label.length * 6 + 14;
  const animated = motionAllowed();

  return (
    <g>
      <defs>
        <marker id={markerId} markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" fill={color} />
        </marker>
      </defs>
      <g>
        {animated && (
          <animate attributeName="opacity" values="1;1;0" keyTimes="0;0.75;1" dur={`${durationMs}ms`} fill="freeze" />
        )}
        <line
          data-testid={`handoff-${id}`}
          x1={start.x}
          y1={start.y}
          x2={end.x}
          y2={end.y}
          stroke={color}
          strokeWidth={3}
          markerEnd={`url(#${markerId})`}
        />
        <g transform={`translate(${mid.x - labelW / 2}, ${mid.y - 11})`}>
          <rect width={labelW} height={22} rx={11} fill="#fff" stroke={color} strokeWidth={1.5} />
          <text x={labelW / 2} y={15} textAnchor="middle" fontSize={11} fontWeight={600} fill={color}>
            {label}
          </text>
        </g>
        <g>
          <rect x={-7} y={-9} width={14} height={18} rx={1.5} fill="#fff" stroke={color} strokeWidth={2} />
          {animated ? (
            <animateMotion dur="1.2s" path={`M${start.x},${start.y} L${end.x},${end.y}`} fill="freeze" />
          ) : (
            <animateTransform attributeName="transform" type="translate" values={`${end.x} ${end.y}`} dur="0.01s" fill="freeze" />
          )}
        </g>
      </g>
    </g>
  );
}
