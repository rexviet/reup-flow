import { Icon } from '../components/Icon';
import { STAGE_TONES, stageText, TONE_CLASSES, TONE_ICONS } from './format';
import type { VideoListItem } from '../api/types';

export function StageBadge({ item, testId }: { item: Pick<VideoListItem, 'stage' | 'step' | 'unknown'>; testId?: string }) {
  const tone = STAGE_TONES[item.stage] ?? 'neutral';
  return (
    <span
      data-testid={testId}
      className={`inline-flex w-max max-w-full items-center gap-1 rounded px-1.5 py-0.5 font-mono text-xs font-semibold ring-1 ring-inset ${TONE_CLASSES[tone]}`}
    >
      <Icon name={TONE_ICONS[tone]} size={13} />
      <span>{stageText(item)}</span>
    </span>
  );
}
