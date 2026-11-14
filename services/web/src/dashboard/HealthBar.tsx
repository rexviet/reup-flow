import { Icon, type IconName } from '../components/Icon';
import { formatClock, formatDateTime, UPDATE_RENDER_MACHINE } from './format';
import type { HealthItem, HealthReport, HealthState, HostWorkerView } from '../api/types';

const TONE: Record<HealthState, { box: string; dot: string; text: string; word: string; icon: IconName }> = {
  ok: { box: 'bg-surface-container-low', dot: 'bg-health-ok', text: 'text-status-success-text', word: 'Ổn', icon: 'check-circle' },
  degraded: { box: 'bg-status-warning-bg/60 ring-1 ring-inset ring-status-warning-border', dot: 'bg-health-degraded', text: 'text-status-warning-text', word: 'Suy giảm', icon: 'warning' },
  down: { box: 'bg-status-error-bg/60 ring-1 ring-inset ring-status-error-border', dot: 'bg-health-down', text: 'text-status-error-text', word: 'Hỏng', icon: 'x-circle' },
  unknown: { box: 'bg-surface-container-low', dot: 'bg-health-unknown', text: 'text-status-idle-text', word: 'Không rõ', icon: 'circle' },
};

interface Badge {
  id: string;
  label: string;
  item: HealthItem;
}

export function healthBadges(h: HealthReport): Badge[] {
  return [
    { id: 'cookie', label: 'Cookie Douyin', item: h.cookie },
    { id: 'voicestudio', label: 'VoiceStudio', item: h.voicestudio },
    { id: 'stt', label: 'Voice STT', item: h.queues.stt },
    { id: 'tts', label: 'Voice TTS', item: h.queues.tts },
    { id: 'ingest', label: 'Tách nhạc (GPU)', item: h.queues.ingest },
    { id: 'host_worker', label: 'Worker review', item: h.host_worker },
    { id: 'temporal', label: 'Temporal', item: h.temporal },
    { id: 'minio', label: 'MinIO', item: h.minio },
    { id: 'disk', label: 'Đĩa', item: h.disk },
  ];
}

const MACHINE_NAMES: Record<string, string> = {
  'review-worker': 'Máy Mac',
  'render-worker-win': 'Máy Windows render',
};

export function machineName(name: string): string {
  return MACHINE_NAMES[name] ?? name;
}

function versionText(w: HostWorkerView): string {
  if (w.code_version === null) return 'bản cũ (chưa báo phiên bản)';
  if (w.code_version === 'unknown') return 'phiên bản không rõ';
  return `bản ${w.code_version}`;
}

function doingText(w: HostWorkerView, nowMs: number): string {
  if (!w.alive) return `mất kết nối từ ${formatClock(w.seen_at, nowMs)}`;
  if (!w.current) return 'rảnh';
  return `đang render ${w.current.aweme_id}${w.current.progress ? ` ${w.current.progress}` : ''}`;
}

/** «Máy Windows render đang chạy bản cũ (abc1234, bản mới def5678). Trên máy đó, …» */
export function outdatedText(w: HostWorkerView): string {
  const versions =
    w.code_version === null
      ? 'chưa báo phiên bản'
      : [w.code_version, w.latest_code_version && w.latest_code_version !== w.code_version ? `bản mới ${w.latest_code_version}` : null].filter(Boolean).join(', ');
  return `${machineName(w.name)} đang chạy bản cũ (${versions}). ${UPDATE_RENDER_MACHINE}`;
}

/** One short line per machine: alive or since when it is silent, what it does, which code it runs. */
function HostWorkers({ workers, nowMs }: { workers: HostWorkerView[]; nowMs: number }) {
  if (workers.length === 0) return null;
  return (
    <ul data-testid="host-workers" aria-label="Các máy worker" className="flex flex-col gap-1">
      {workers.map((w) => {
        const tone = w.outdated ? TONE.down : w.alive ? TONE.ok : TONE.degraded;
        return (
          <li
            key={w.name}
            data-testid={`host-${w.name}`}
            data-alive={w.alive}
            data-outdated={w.outdated}
            className={`flex min-w-0 flex-col gap-0.5 rounded px-2 py-1 text-xs ${tone.box}`}
          >
            <span className="flex min-w-0 flex-wrap items-center gap-x-1.5">
              <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${w.alive ? 'bg-health-ok' : 'bg-health-down'}`} />
              <span className="font-medium text-text-primary">{machineName(w.name)}</span>
              <span className="text-text-secondary">· {doingText(w, nowMs)}</span>
              <span className={`font-mono text-[11px] ${w.outdated ? tone.text : 'text-text-muted'}`}>· {versionText(w)}</span>
            </span>
            {w.outdated ? (
              <span role="alert" className="flex items-start gap-1 font-semibold text-status-error-text">
                <Icon name="warning" size={12} className="mt-0.5 shrink-0" />
                {outdatedText(w)}
              </span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export function HealthBar({ health, error }: { health: HealthReport | null; error: Error | null }) {
  if (!health) {
    return (
      <div data-testid="health-bar" className="card text-xs text-text-secondary">
        {error ? `Không đọc được sức khỏe hệ thống: ${error.message}` : 'Sức khỏe: đang tải…'}
      </div>
    );
  }
  return (
    <section data-testid="health-bar" aria-label="Sức khỏe hệ thống" className="flex flex-col gap-2 rounded-xl bg-surface-layer-1 p-2 shadow-md">
      <div className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between">
        <ul className="grid w-full grid-cols-2 gap-1 sm:grid-cols-4 xl:grid-cols-8">
          {healthBadges(health).map(({ id, label, item }) => {
            const tone = TONE[item.status];
            return (
              <li
                key={id}
                data-testid={`health-${id}`}
                data-state={item.status}
                title={item.reason}
                className={`flex min-w-0 items-center gap-2 rounded px-2 py-1.5 ${tone.box}`}
              >
                <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${tone.dot}`} />
                <span className="flex min-w-0 flex-col">
                  <span className="truncate text-xs font-medium text-text-primary">{label}</span>
                  <span className={`flex items-center gap-1 font-mono text-[11px] font-semibold ${tone.text}`}>
                    <Icon name={tone.icon} size={11} />
                    {tone.word}
                    <span className="sr-only">: {item.reason}</span>
                  </span>
                </span>
              </li>
            );
          })}
        </ul>
        <p className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs text-text-muted">
          <span className="inline-flex items-center gap-1">
            <Icon name="clock" size={13} />
            Cập nhật {formatDateTime(health.generatedAt)}
          </span>
          {error ? <span className="text-status-warning-text">Mất kết nối, đang hiện dữ liệu cũ.</span> : null}
        </p>
      </div>
      <HostWorkers workers={health.host_workers ?? []} nowMs={Date.parse(health.generatedAt)} />
    </section>
  );
}
