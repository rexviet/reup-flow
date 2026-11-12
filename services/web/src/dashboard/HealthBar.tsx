import { Icon, type IconName } from '../components/Icon';
import { formatDateTime } from './format';
import type { HealthItem, HealthReport, HealthState } from '../api/types';

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
    { id: 'host_worker', label: 'Worker review', item: h.host_worker },
    { id: 'temporal', label: 'Temporal', item: h.temporal },
    { id: 'minio', label: 'MinIO', item: h.minio },
    { id: 'disk', label: 'Đĩa', item: h.disk },
  ];
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
    <section
      data-testid="health-bar"
      aria-label="Sức khỏe hệ thống"
      className="flex flex-col gap-2 rounded-xl bg-surface-layer-1 p-2 shadow-md xl:flex-row xl:items-center xl:justify-between"
    >
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
    </section>
  );
}
