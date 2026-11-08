import type { WorkspaceEvent, WorkspaceEventKind } from '../api/types';
import { deskLabel } from './workspace.layout';

const KIND_LABELS: Record<WorkspaceEventKind, string> = {
  activity_started: 'bắt đầu',
  activity_retrying: 'thử lại',
  activity_completed: 'xong',
  activity_failed: 'lỗi',
  llm_call_recorded: 'gọi LLM',
  gate_decision: 'kết quả',
  handoff: 'chuyển giao',
};

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString('vi-VN', { hour12: false });
}

/** The durable record; bubbles and lines are only the transient layer on top of it. */
export function EventLog({ events }: { events: WorkspaceEvent[] }) {
  return (
    <section aria-label="Nhật ký sự kiện">
      <h2 className="mb-2 text-sm font-semibold text-gray-900">Nhật ký sự kiện</h2>
      {events.length === 0 ? (
        <p className="text-sm text-gray-500">Chưa có sự kiện nào.</p>
      ) : (
        <ol className="flex max-h-96 flex-col gap-1 overflow-y-auto text-xs">
          {events.map((e) => (
            <li key={e.id} className="rounded border border-gray-100 bg-white px-2 py-1">
              <span className="text-gray-600">{clock(e.at)}</span>{' '}
              <span className="mr-1 font-medium text-gray-700">{KIND_LABELS[e.kind]}</span>
              {e.agentId && <span className="mr-1 text-gray-600"> · {deskLabel(e.agentId)}</span>}
              {e.awemeId && <span className="text-gray-600"> · …{e.awemeId.slice(-6)}</span>}
              <span className="block text-gray-600">{e.detail}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
