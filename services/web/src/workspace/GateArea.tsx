import type { GateOutcome, WorkspaceRun, WorkspaceSnapshot } from '../api/types';
import { Bubble } from './Bubble';
import { deskLabel, GATE_COLUMNS } from './workspace.layout';

const PHASE_LABELS: Record<string, string> = {
  published: 'đã đăng',
  failed: 'lỗi',
  starting: 'đang khởi động',
};

export function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] ?? deskLabel(phase);
}

interface Props {
  snapshot: WorkspaceSnapshot | null;
  bubble?: { text: string; awemeId?: string };
  selected?: { awemeId: string; generation: number } | null;
  onSelect: (awemeId: string, generation: number) => void;
}

function RunChip({
  run,
  label,
  selected,
  onSelect,
}: {
  run: WorkspaceRun;
  label: string;
  selected: boolean;
  onSelect: Props['onSelect'];
}) {
  return (
    <li>
      <button
        type="button"
        aria-label={`Video ${run.awemeId} lần ${run.generation}: ${label}`}
        onClick={() => onSelect(run.awemeId, run.generation)}
        className={`w-full rounded border bg-white px-2 py-1 text-left text-xs hover:bg-gray-50 ${
          selected ? 'border-blue-500 ring-1 ring-blue-500' : 'border-gray-200'
        }`}
      >
        <span className="text-gray-700">…{run.awemeId.slice(-8)}</span>
        <span className="text-gray-400"> g{run.generation}</span>
        <span className="block text-gray-500">{label}</span>
      </button>
    </li>
  );
}

/**
 * Videos are grouped by what happened to them, not placed at a desk: the four
 * gate decisions each get a column, and every other run (finished, failed, or
 * still moving through the desks) is listed under "Khác" with its phase.
 */
export function GateArea({ snapshot, bubble, selected, onSelect }: Props) {
  const runs = snapshot?.runs ?? [];
  const byOutcome = (o: GateOutcome) => runs.filter((r) => r.outcome === o);
  const others = runs.filter((r) => r.outcome === undefined);
  const isSelected = (r: WorkspaceRun) => selected?.awemeId === r.awemeId && selected.generation === r.generation;

  return (
    <section aria-label="Cổng" className="relative rounded-lg border border-gray-200 bg-gray-50 p-3">
      <h2 className="mb-2 text-sm font-semibold text-gray-900">Cổng</h2>
      {bubble && <Bubble text={bubble.text} awemeId={bubble.awemeId} />}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {GATE_COLUMNS.map(({ outcome, label }) => (
          <div key={outcome} data-outcome={outcome}>
            <h3 className="mb-1 text-xs font-medium text-gray-700">
              {label} ({byOutcome(outcome).length})
            </h3>
            <ul className="flex flex-col gap-1">
              {byOutcome(outcome).map((r) => (
                <RunChip key={`${r.awemeId}:${r.generation}`} run={r} label={label} selected={isSelected(r)} onSelect={onSelect} />
              ))}
            </ul>
          </div>
        ))}
        <div data-outcome="other">
          <h3 className="mb-1 text-xs font-medium text-gray-700">Khác ({others.length})</h3>
          <ul className="flex flex-col gap-1">
            {others.map((r) => (
              <RunChip key={`${r.awemeId}:${r.generation}`} run={r} label={phaseLabel(r.phase)} selected={isSelected(r)} onSelect={onSelect} />
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
