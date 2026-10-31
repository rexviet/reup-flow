import type { WorkspaceSnapshot } from '../api/types';

export interface TopBarProps {
  snapshot: WorkspaceSnapshot | null;
  /** Seconds the data is behind because the last poll failed; 0 when current. */
  staleSeconds: number;
  loading: boolean;
}

const SOURCE_NAMES = { temporal: 'Temporal', postgres: 'Postgres' } as const;

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString('vi-VN', { hour12: false });
}

/**
 * "Dữ liệu thật" appears only when both sources answered on the latest poll.
 * Anything less names what is wrong, so the page never reads as live when it is not.
 */
export function TopBar({ snapshot, staleSeconds, loading }: TopBarProps) {
  if (!snapshot) {
    return (
      <p className="text-sm text-gray-500">
        {loading ? 'Đang tải…' : 'Không tải được dữ liệu workspace.'}
        {staleSeconds > 0 && ` (lỗi ${staleSeconds} giây)`}
      </p>
    );
  }
  const down = (Object.keys(SOURCE_NAMES) as Array<keyof typeof SOURCE_NAMES>).filter(
    (k) => snapshot.sources[k] === 'down',
  );
  const live = down.length === 0 && staleSeconds === 0;

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      {live && <span className="font-medium text-green-700">Dữ liệu thật</span>}
      {(Object.keys(SOURCE_NAMES) as Array<keyof typeof SOURCE_NAMES>).map((k) => (
        <span key={k} className={snapshot.sources[k] === 'ok' ? 'text-gray-600' : 'font-medium text-red-700'}>
          {SOURCE_NAMES[k]}: {snapshot.sources[k] === 'ok' ? 'ổn' : 'lỗi'}
        </span>
      ))}
      <span className="text-gray-500">Cập nhật lúc {clock(snapshot.generatedAt)}</span>
      {staleSeconds > 0 && <span className="font-medium text-amber-700">cũ {staleSeconds} giây</span>}
    </div>
  );
}
