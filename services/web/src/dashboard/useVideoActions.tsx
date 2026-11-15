import { useCallback, type ReactNode } from 'react';
import type { ActionRequest, ActionResult, DashboardActionName, VideoListItem } from '../api/types';
import { useConfirm } from './ConfirmDialog';
import { ACTION_LABELS, CONFIRMED_ACTIONS } from './format';
import { useActionRunner, type ActionRunner } from './useDashboard';

export type ActionTarget = Pick<VideoListItem, 'aweme_id' | 'current_gen' | 'revision' | 'mismatch_count'>;
export type ActionExtras = Pick<ActionRequest, 'note' | 'post_url' | 'platform'>;

/** Actions the server pins to the generation the admin was looking at (single video only). */
const GENERATION_PINNED: readonly DashboardActionName[] = [
  'approve',
  'return',
  'rerender',
  'mark_published',
  'mark_unpublished',
  'open_studio',
];

export interface VideoActions {
  runner: ActionRunner;
  dialog: ReactNode;
  perform: (action: DashboardActionName, targets: ActionTarget[], extras?: ActionExtras) => Promise<ActionResult | null>;
}

/**
 * One entry point for every action button: asks first when the action costs money or machine time
 * (naming how many videos), asks again on its own when an approval would leave mismatched cues,
 * then posts. `onSettled` re-fetches whatever the page polls.
 */
export function useVideoActions(onSettled: () => void): VideoActions {
  const runner = useActionRunner(onSettled);
  const { ask, dialog } = useConfirm();
  const { run } = runner;

  const perform = useCallback<VideoActions['perform']>(
    async (action, targets, extras) => {
      if (targets.length === 0) return null;
      if (CONFIRMED_ACTIONS.includes(action)) {
        const [only] = targets;
        const detail =
          action === 'approve' && targets.length === 1
            ? ` (revision ${only.revision ?? '?'}, ${only.mismatch_count ?? 0} chỗ lệch)`
            : '';
        const ok = await ask(`${ACTION_LABELS[action]} cho ${targets.length} video${detail}?`);
        if (!ok) return null;
      }
      const body: ActionRequest = { action, aweme_ids: targets.map((t) => t.aweme_id), ...extras };
      if (targets.length === 1) {
        const [only] = targets;
        if (GENERATION_PINNED.includes(action) && only.current_gen !== null) body.generation = only.current_gen;
        if (action === 'approve') {
          if (only.revision === null) return null;
          body.revision = only.revision;
          if ((only.mismatch_count ?? 0) > 0) {
            const ok = await ask(`Còn ${only.mismatch_count} chỗ lời đọc lệch phụ đề. Vẫn duyệt bản này?`);
            if (!ok) return null;
            body.confirm_mismatch = true;
          }
        }
      }
      return run(body);
    },
    [ask, run],
  );

  return { runner, dialog, perform };
}
