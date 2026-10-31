import { useCallback } from 'react';
import { getRunDetail } from '../api/client';
import type { RunDetail } from '../api/types';
import { usePolling } from '../hooks/usePolling';
import { phaseLabel } from './GateArea';
import { deskLabel, outcomeLabel } from './workspace.layout';

const PHASE_STATE_LABELS: Record<RunDetail['phases'][number]['state'], string> = {
  running: 'đang chạy',
  retrying: 'đang thử lại',
  done: 'xong',
  failed: 'lỗi',
};

const usd = (n: number): string => `$${n.toFixed(4)}`;

export function RunDetailView({ detail }: { detail: RunDetail }) {
  const down = (Object.keys(detail.sources) as Array<keyof RunDetail['sources']>).filter(
    (k) => detail.sources[k] === 'down',
  );
  return (
    <div className="flex flex-col gap-4 text-sm">
      {down.length > 0 && (
        <p role="alert" className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-800">
          Không đọc được {down.map((k) => (k === 'temporal' ? 'Temporal' : 'Postgres')).join(' và ')}; dữ liệu bên dưới có thể thiếu.
        </p>
      )}

      <section aria-label="Kết quả cổng">
        <h3 className="mb-1 font-semibold text-gray-900">Kết quả</h3>
        <p data-testid="run-result">
          {detail.outcome ? outcomeLabel(detail.outcome) : detail.status ? phaseLabel(detail.status) : 'chưa có kết quả'}
        </p>
      </section>

      <section aria-label="Tiến độ">
        <h3 className="mb-1 font-semibold text-gray-900">Tiến độ</h3>
        {detail.phases.length === 0 ? (
          <p className="text-gray-500">Chưa có pha nào.</p>
        ) : (
          <ol className="flex flex-col gap-1">
            {detail.phases.map((p) => (
              <li key={p.agentId} className="flex justify-between gap-2">
                <span>{deskLabel(p.agentId)}</span>
                <span className="text-gray-500">
                  {PHASE_STATE_LABELS[p.state]}
                  {p.activities > 1 && ` · ${p.activities} bước`}
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section aria-label="Chi phí LLM">
        <h3 className="mb-1 font-semibold text-gray-900">Chi phí LLM</h3>
        <table className="w-full text-xs">
          <tbody>
            {detail.costByStage.map((c) => (
              <tr key={c.stage}>
                <td className="truncate pr-2 text-gray-600">{c.stage}</td>
                <td className="pr-2 text-right text-gray-400">{c.calls}×</td>
                <td className="text-right">{usd(c.costUsd)}</td>
              </tr>
            ))}
            <tr className="border-t border-gray-200 font-medium">
              <td className="pr-2">Tổng</td>
              <td />
              <td className="text-right" data-testid="run-total-cost">
                {usd(detail.llmCostUsd)}
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section aria-label="Thời gian GPU">
        <h3 className="mb-1 font-semibold text-gray-900">Thời gian GPU</h3>
        <p>
          STT {detail.gpuSecondsEstimate.stt.toFixed(1)} giây · TTS {detail.gpuSecondsEstimate.tts.toFixed(1)} giây
        </p>
        <p className="text-xs text-gray-500">
          Ước tính từ thời gian activity (gồm cả lúc chờ worker), chưa phải hoá đơn.
        </p>
      </section>
    </div>
  );
}

export interface RunPanelProps {
  awemeId: string;
  generation: number;
  onClose: () => void;
}

/** Mount with `key={awemeId + generation}` so a new selection never shows the old run's numbers. */
export function RunPanel({ awemeId, generation, onClose }: RunPanelProps) {
  const fetcher = useCallback(() => getRunDetail(awemeId, generation), [awemeId, generation]);
  const { data, error, loading } = usePolling(fetcher, 2000, { pauseWhenHidden: true });

  return (
    <aside aria-label="Chi tiết video" className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">Video …{awemeId.slice(-8)}</h2>
          <p className="text-xs text-gray-500">lần chạy {generation}</p>
        </div>
        <button type="button" onClick={onClose} className="rounded px-2 py-1 text-xs text-gray-600 hover:bg-gray-100">
          Đóng
        </button>
      </div>
      {data ? (
        <RunDetailView detail={data} />
      ) : loading ? (
        <p className="text-sm text-gray-500">Đang tải…</p>
      ) : (
        <p role="alert" className="text-sm text-red-700">
          Không tải được chi tiết{error ? `: ${error.message}` : ''}.
        </p>
      )}
      {data && error && <p className="mt-2 text-xs text-amber-700">Làm mới lỗi, đang hiện dữ liệu cũ.</p>}
    </aside>
  );
}
