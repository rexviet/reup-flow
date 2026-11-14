import { useRef, useState } from 'react';
import { moveCollection } from '../api/client';
import type { CollectionItem, FolderExportView, MediaKind, VideoDetailResponse } from '../api/types';
import { ActionNotice } from './ActionNotice';
import { Icon } from '../components/Icon';
import {
  ACTION_LABELS,
  ACTIONS_BY_STAGE,
  COLLECTION_KIND_LABELS,
  collectionText,
  formatDateTime,
  formatUsd,
  TONE_CLASSES,
  TONE_ICONS,
  type StageTone,
} from './format';
import { StageBadge } from './StageBadge';
import { useMediaLinks, useThumbLinks } from './useMediaLinks';
import { SmallThumb } from './VideoThumb';
import { useVideoActions, type ActionTarget } from './useVideoActions';
import { VideoPlayer } from './VideoPlayer';
import { useCollections, useVideoDetail } from './useDashboard';

const STEP_STATE: Record<string, string> = {
  running: 'đang chạy',
  retrying: 'thử lại',
  done: 'xong',
  failed: 'lỗi',
  cancelled: 'đã hủy',
};

const STEP_TONE: Record<string, StageTone> = {
  running: 'active',
  retrying: 'warn',
  done: 'good',
  failed: 'bad',
  cancelled: 'neutral',
};

const STUDIO_STATE: Record<string, string> = {
  pending: 'đang chờ máy mở',
  done: 'đã mở',
  failed: 'mở lỗi',
};

const FOLDER_STATE: Record<string, string> = {
  pending: 'Đang chờ máy Mac lưu',
  exporting: 'Đang lưu',
  done: 'Đã lưu',
  failed: 'Lỗi',
};

/** What the newest save request came to: the files written when done, the reason when it failed. */
function folderExportText(f: FolderExportView): string {
  const state = FOLDER_STATE[f.state] ?? f.state;
  if (f.state === 'done' && f.files && f.files.length > 0) return `${state}: ${f.files.join(', ')}`;
  if (f.state === 'failed' && f.error) return `${state}: ${f.error}`;
  return state;
}

const HISTORY_LABELS: Record<string, string> = { ...ACTION_LABELS, move_collection: 'Chuyển collection' };

function withDownload(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}download=1`;
}

export function VideoDetail({ awemeId }: { awemeId: string }) {
  const detail = useVideoDetail(awemeId);
  const collections = useCollections();
  const actions = useVideoActions(detail.refresh);
  // A slow Postgres answers `video: null` now and then; keep the last full page (player, typed text) up.
  const lastGood = useRef<VideoDetailResponse | null>(null);
  if (detail.data?.video) lastGood.current = detail.data;
  const down = detail.data !== null && detail.data.video === null;
  const d = down && lastGood.current ? { ...lastGood.current, sources: detail.data!.sources } : detail.data;

  return (
    <div className="flex flex-col gap-4">
      <a href="#/" className="inline-flex min-h-11 w-max items-center gap-1.5 text-sm text-secondary hover:underline">
        <Icon name="arrow-left" />
        Quay lại dashboard
      </a>
      {!d ? (
        <p className="text-sm text-text-secondary">{detail.error ? `Không tải được video: ${detail.error.message}` : 'Đang tải…'}</p>
      ) : d.video === null ? (
        <p role="alert" className="rounded bg-status-warning-bg p-3 text-sm text-status-warning-text">
          Không đọc được Postgres nên chưa có chi tiết video {awemeId}.
        </p>
      ) : (
        <Loaded
          d={d}
          awemeId={awemeId}
          actions={actions}
          stale={detail.error !== null || down}
          collections={collections.data?.items ?? []}
          onChanged={detail.refresh}
        />
      )}
      {actions.dialog}
    </div>
  );
}

function Loaded({
  d,
  awemeId,
  actions,
  stale,
  collections,
  onChanged,
}: {
  d: VideoDetailResponse;
  awemeId: string;
  actions: ReturnType<typeof useVideoActions>;
  stale: boolean;
  collections: CollectionItem[];
  onChanged: () => void;
}) {
  const video = d.video!;
  const stage = d.stage!;
  const gen = d.current_gen ?? null;
  const render = d.render ?? null;
  // Videos rendered before generations existed have no render row but still have a final file.
  const mayHaveFinal = render?.final_ready === true || stage === 'rendered' || stage === 'published';
  const kinds: MediaKind[] = ['source', ...(mayHaveFinal ? (['final'] as const) : []), ...(render?.srt_ready ? (['srt'] as const) : [])];
  const media = useMediaLinks(awemeId, gen, kinds);
  const thumb = useThumbLinks([awemeId])[awemeId];

  const [returnNote, setReturnNote] = useState('');
  const [postUrl, setPostUrl] = useState('');
  const [platform, setPlatform] = useState('');
  const [moveTarget, setMoveTarget] = useState('');
  const [moveError, setMoveError] = useState<string | null>(null);
  const [moving, setMoving] = useState(false);
  const [pickedGen, setPickedGen] = useState<number | null>(null);

  // Every generation with a rendered final can be saved; a final from before generations existed
  // has no render row, so the current generation stands for it.
  const renderedGens = (d.generations ?? []).filter((g) => g.render?.state === 'rendered').map((g) => g.generation);
  const saveGens = renderedGens.length > 0 ? renderedGens : mayHaveFinal && gen !== null ? [gen] : [];
  const saveGen = [pickedGen, gen].find((g) => g !== null && saveGens.includes(g)) ?? saveGens[0] ?? null;
  const folder = collections.find((c) => c.id === d.collection?.id);
  // Unknown (collections not loaded yet) leaves the button on; the server still refuses a collection without a folder.
  const noFolder = folder?.save_subdir === null;

  function moveTo(collectionId: string) {
    setMoving(true);
    setMoveError(null);
    void moveCollection([awemeId], collectionId)
      .then((res) => {
        if (res.skipped[0]) setMoveError(res.skipped[0].reason);
        else setMoveTarget('');
      })
      .catch((err: unknown) => setMoveError(err instanceof Error ? err.message : String(err)))
      .finally(() => {
        setMoving(false);
        onChanged();
      });
  }

  const target: ActionTarget = {
    aweme_id: awemeId,
    current_gen: gen,
    revision: d.review_status?.revision ?? null,
    mismatch_count: d.review_status?.mismatchCount ?? null,
  };
  const available = ACTIONS_BY_STAGE[stage] ?? [];
  const has = (a: (typeof available)[number]) => available.includes(a);
  const busy = actions.runner.busy;
  const validPostUrl = /^https?:\/\/\S+$/.test(postUrl.trim());
  const lastStudio = (d.dashboard_actions ?? []).find((a) => a.action === 'open_studio');
  const studioSkipped = actions.runner.result?.action === 'open_studio' ? actions.runner.result.skipped[0] : undefined;

  return (
    <>
      <header className="card flex items-start gap-3">
        <SmallThumb url={thumb} size="md" />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-mono text-lg font-bold tracking-tight text-text-primary">{video.aweme_id}</h1>
            {gen !== null ? <span className="rounded bg-primary-container px-1.5 py-0.5 font-mono text-[11px] font-semibold text-on-primary-container">g{gen}</span> : null}
            <StageBadge testId="detail-stage" item={{ stage, step: d.step, unknown: d.unknown }} />
            {d.review_status?.mismatchCount ? (
              <span className="inline-flex items-center gap-1 rounded bg-status-error-bg px-1.5 py-0.5 font-mono text-[11px] font-semibold text-status-error-text">
                <Icon name="warning" size={12} />
                {d.review_status.mismatchCount} cue lệch
              </span>
            ) : null}
            <span className="inline-flex max-w-full items-center gap-1 break-all rounded bg-surface-container px-1.5 py-0.5 font-mono text-xs text-text-primary">
              <Icon name="user" size={12} />
              {d.channel?.nickname ?? d.channel?.sec_user_id}
            </span>
            {d.collection ? (
              <span
                data-testid="detail-collection"
                title={COLLECTION_KIND_LABELS[d.collection.kind] ?? d.collection.kind}
                className="inline-flex max-w-full items-center gap-1 break-all rounded bg-surface-container px-1.5 py-0.5 text-xs text-text-primary"
              >
                <Icon name="tag" size={12} />
                {collectionText(d.collection, video.mix_episode)}
              </span>
            ) : null}
          </div>
          <p className="text-sm text-text-primary">{video.desc ?? '(không có mô tả)'}</p>
          {stale ? <p className="text-xs text-status-warning-text">Mất kết nối, đang hiện dữ liệu cũ.</p> : null}
          {d.sources.postgres === 'down' || d.sources.temporal === 'down' ? (
            <p role="alert" className="rounded bg-status-warning-bg p-2 text-xs text-status-warning-text">
              {d.sources.postgres === 'down' ? 'Một phần dữ liệu Postgres không đọc được. ' : ''}
              {d.sources.temporal === 'down' ? 'Không đọc được Temporal nên chưa có timeline.' : ''}
            </p>
          ) : null}
        </div>
      </header>

      {(d.attention ?? []).length > 0 ? (
        <ul className="rounded-xl bg-status-error-bg p-3 text-sm text-status-error-text ring-1 ring-status-error-border">
          {d.attention!.map((a) => (
            <li key={a.code}>
              {a.reason} <span className="text-text-secondary">{a.action}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <section aria-label="Video" className="grid gap-3 md:grid-cols-2">
        <div className="card">
          <p className="eyebrow mb-1.5">Video gốc</p>
          <VideoPlayer url={media.links.source?.url} label="Video gốc" />
        </div>
        <div className="card">
          <p className="eyebrow mb-1.5">Bản cuối</p>
          {mayHaveFinal && media.links.final?.available !== false ? (
            <>
              <VideoPlayer url={media.links.final?.url} label="Bản cuối" />
              {media.links.final?.note ? <p className="mt-1 text-xs text-text-muted">{media.links.final.note}</p> : null}
            </>
          ) : (
            <p className="flex aspect-video items-center justify-center rounded bg-surface-container-lowest text-xs text-text-muted">Chưa có bản cuối.</p>
          )}
        </div>
        {media.error ? <p className="text-xs text-status-warning-text md:col-span-2">Không lấy được link video: {media.error}</p> : null}
      </section>

      <section aria-label="Thao tác" className="card">
        <h2 className="card-title mb-2">Thao tác</h2>
        <div className="flex flex-wrap items-center gap-2">
          {has('localize') ? (
            <button type="button" disabled={busy} onClick={() => void actions.perform('localize', [target])} className="btn btn-neutral">
              {ACTION_LABELS.localize}
            </button>
          ) : null}
          {has('retry_download') ? (
            <button type="button" disabled={busy} onClick={() => void actions.perform('retry_download', [target])} className="btn btn-neutral">
              {ACTION_LABELS.retry_download}
            </button>
          ) : null}
          {has('resume') ? (
            <button
              type="button"
              disabled={busy}
              title="Giữ các bước đã xong, chạy lại từ bước bị lỗi."
              onClick={() => void actions.perform('resume', [target])}
              className="btn btn-neutral"
            >
              {ACTION_LABELS.resume}
            </button>
          ) : null}
          {has('rerun') ? (
            <button type="button" disabled={busy} title="Chạy lại từ đầu, tạo một lượt xử lý mới." onClick={() => void actions.perform('rerun', [target])} className="btn btn-neutral">
              {ACTION_LABELS.rerun}
            </button>
          ) : null}
          {has('open_studio') ? (
            <button type="button" disabled={busy} onClick={() => void actions.perform('open_studio', [target])} className="btn btn-ghost">
              Mở Studio
            </button>
          ) : null}
          {has('approve') ? (
            <button
              type="button"
              disabled={busy || target.revision === null || d.review_status?.dirty === true}
              title={
                target.revision === null
                  ? 'Chưa có bản review để duyệt.'
                  : d.review_status?.dirty === true
                    ? 'Project có thay đổi chưa lưu; lưu trong Studio trước.'
                    : undefined
              }
              onClick={() => void actions.perform('approve', [target])}
              className="btn btn-primary"
            >
              {ACTION_LABELS.approve}
            </button>
          ) : null}
          {has('rerender') ? (
            <button type="button" disabled={busy} onClick={() => void actions.perform('rerender', [target])} className="btn btn-ghost">
              {ACTION_LABELS.rerender}
            </button>
          ) : null}
          {has('mark_unpublished') ? (
            <button type="button" disabled={busy} onClick={() => void actions.perform('mark_unpublished', [target])} className="btn btn-ghost">
              {ACTION_LABELS.mark_unpublished}
            </button>
          ) : null}
          {media.links.final?.url ? (
            <a href={withDownload(media.links.final.url)} className="btn btn-ghost">
              Tải MP4
            </a>
          ) : null}
          {media.links.srt?.url ? (
            <a href={withDownload(media.links.srt.url)} className="btn btn-ghost">
              Tải SRT
            </a>
          ) : null}
        </div>

        {saveGens.length > 0 || d.folder_export ? (
          <div className="mt-3 flex flex-col gap-1.5">
            {saveGens.length > 0 ? (
              <div className="flex flex-wrap items-center gap-2">
                <label htmlFor="save-generation" className="sr-only">
                  Generation để lưu
                </label>
                <select
                  id="save-generation"
                  value={saveGen ?? ''}
                  onChange={(e) => setPickedGen(Number(e.target.value))}
                  className="field w-auto"
                >
                  {saveGens.map((g) => (
                    <option key={g} value={g}>
                      g{g}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={busy || noFolder || saveGen === null}
                  onClick={() => saveGen !== null && void actions.runner.run({ action: 'save_to_folder', aweme_ids: [awemeId], generation: saveGen })}
                  className="btn btn-neutral"
                >
                  <Icon name="download" />
                  {ACTION_LABELS.save_to_folder}
                </button>
              </div>
            ) : null}
            {saveGens.length > 0 && noFolder ? (
              <p data-testid="folder-hint" className="text-xs text-status-warning-text">
                Collection «{d.collection?.name}» chưa có thư mục lưu; đặt thư mục trong panel Collections ở dashboard.
              </p>
            ) : null}
            {/* Always the newest request as polled; a skipped click is reported by the action notice below. */}
            {d.folder_export ? (
              <p data-testid="folder-export-status" className="text-xs text-text-secondary">
                Lưu gần nhất (g{d.folder_export.generation}, {formatDateTime(d.folder_export.requested_at)}): {folderExportText(d.folder_export)}
              </p>
            ) : null}
          </div>
        ) : null}

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label htmlFor="move-collection" className="sr-only">
            Chuyển sang collection
          </label>
          <select id="move-collection" value={moveTarget} onChange={(e) => setMoveTarget(e.target.value)} className="field w-auto max-w-[16rem]">
            <option value="">Chọn collection…</option>
            {collections
              .filter((c) => c.id !== d.collection?.id)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} ({COLLECTION_KIND_LABELS[c.kind] ?? c.kind})
                </option>
              ))}
          </select>
          <button type="button" disabled={moving || moveTarget === ''} onClick={() => moveTo(moveTarget)} className="btn btn-ghost">
            Chuyển collection
          </button>
          {moveError ? (
            <span role="alert" className="text-xs text-status-error-text">
              {moveError}
            </span>
          ) : null}
        </div>

        {has('open_studio') ? (
          <p data-testid="studio-status" className="mt-2 text-xs text-text-secondary">
            Studio mở trên máy chạy dashboard.{' '}
            {studioSkipped
              ? studioSkipped.reason
              : d.studio_request
                ? `Yêu cầu gần nhất (g${d.studio_request.generation}, ${formatDateTime(d.studio_request.requestedAt)}): ${STUDIO_STATE[d.studio_request.state] ?? d.studio_request.state}${d.studio_request.error ? ` - ${d.studio_request.error}` : ''}.`
                : lastStudio
                  ? `Yêu cầu gần nhất: ${formatDateTime(lastStudio.created_at)}.`
                  : 'Chưa có yêu cầu mở Studio.'}
          </p>
        ) : null}

        {has('return') ? (
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <label htmlFor="return-note" className="sr-only">
              Ghi chú trả về
            </label>
            <input
              id="return-note"
              type="text"
              value={returnNote}
              onChange={(e) => setReturnNote(e.target.value)}
              placeholder="Ghi chú khi trả về"
              maxLength={2000}
              className="field flex-1"
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => void actions.perform('return', [target], returnNote.trim() ? { note: returnNote.trim() } : undefined).then((r) => r && setReturnNote(''))}
              className="btn btn-danger"
            >
              {ACTION_LABELS.return}
            </button>
          </div>
        ) : null}

        {has('mark_published') ? (
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <label htmlFor="post-url" className="sr-only">
              Link bài đăng
            </label>
            <input id="post-url" type="url" value={postUrl} onChange={(e) => setPostUrl(e.target.value)} placeholder="Link bài đã đăng (https://…)" className="field flex-1" />
            <label htmlFor="post-platform" className="sr-only">
              Nền tảng
            </label>
            <input id="post-platform" type="text" value={platform} onChange={(e) => setPlatform(e.target.value)} placeholder="Nền tảng" maxLength={50} className="field sm:w-36" />
            <button
              type="button"
              disabled={busy || !validPostUrl}
              onClick={() =>
                void actions.perform('mark_published', [target], {
                  post_url: postUrl.trim(),
                  ...(platform.trim() ? { platform: platform.trim() } : {}),
                })
              }
              className="btn btn-ghost"
            >
              {ACTION_LABELS.mark_published}
            </button>
          </div>
        ) : null}

        <div className="mt-3">
          <ActionNotice runner={actions.runner} />
        </div>
      </section>

      <div className="grid gap-3 lg:grid-cols-3">
        <div className="flex flex-col gap-3 lg:col-span-2">
      <section aria-label="Review" className="card text-sm">
        <h2 className="card-title mb-2">Review</h2>
        {d.review_status ? (
          <p data-testid="mismatch" className="text-text-primary">
            Số cue lệch: <strong>{d.review_status.mismatchCount ?? '-'}</strong>
            {d.review_status.dirty ? ' (project có thay đổi chưa lưu)' : ''}
            <span className="ml-2 font-mono text-xs text-text-muted">revision {d.review_status.revision ?? '-'}</span>
          </p>
        ) : (
          <p className="text-text-secondary">Chưa có trạng thái review.</p>
        )}
        {d.verdict ? (
          <details className="mt-2">
            <summary className="cursor-pointer text-xs text-text-secondary">Verdict của Reviewer</summary>
            <pre className="mt-1 max-h-64 overflow-auto rounded bg-surface-container-lowest p-2 font-mono text-xs text-text-primary">{JSON.stringify(d.verdict, null, 2)}</pre>
          </details>
        ) : (
          <p className="mt-1 text-xs text-text-muted">Chưa có verdict.</p>
        )}
      </section>

      <section aria-label="Timeline" className="card">
        <h2 className="card-title mb-2">Timeline</h2>
        {d.timeline && d.timeline.length > 0 ? (
          <ol className="grid max-h-[32rem] grid-cols-1 gap-1.5 overflow-y-auto sm:grid-cols-2 xl:grid-cols-3">
            {d.timeline.map((s, i) => {
              const tone = STEP_TONE[s.state] ?? 'neutral';
              return (
                <li key={s.id} className="flex flex-col gap-1 rounded bg-surface-container p-2 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-[11px] text-text-muted">{i + 1}.</span>
                    <span className={`inline-flex items-center gap-1 rounded px-1 font-mono text-[11px] font-semibold ${TONE_CLASSES[tone]}`}>
                      <Icon name={TONE_ICONS[tone]} size={11} />
                      {STEP_STATE[s.state] ?? s.state}
                      {s.attempt && s.attempt > 1 ? ` (lần ${s.attempt})` : ''}
                    </span>
                  </div>
                  <span className="break-words font-mono text-xs font-semibold text-text-primary">{s.activity}</span>
                  <span className="font-mono text-[11px] text-text-secondary">{formatDateTime(s.startedAt)}</span>
                </li>
              );
            })}
          </ol>
        ) : (
          <p className="text-sm text-text-secondary">Chưa có timeline.</p>
        )}
      </section>

        </div>
        <div className="flex flex-col gap-3">
      <section aria-label="Chi phí và generation" className="card text-sm">
        <h2 className="card-title mb-2">Chi phí LLM</h2>
        <p data-testid="llm-cost">Tổng: {formatUsd(d.llm_cost?.total_usd ?? 0)}</p>
        {(d.llm_cost?.by_stage ?? []).length > 0 ? (
          <ul className="mt-1 text-xs text-text-secondary">
            {d.llm_cost!.by_stage.map((s) => (
              <li key={s.stage}>
                {s.stage}: {s.calls} lần, {formatUsd(s.cost_usd)}
              </li>
            ))}
          </ul>
        ) : null}
        <h2 className="card-title mb-2 mt-4">Generation</h2>
        <ul className="divide-y divide-border-subtle/60">
          {(d.generations ?? []).map((g) => (
            <li key={g.generation} className="flex flex-wrap items-center justify-between gap-2 py-1.5 text-xs text-text-secondary">
              <span className={g.is_current ? 'font-semibold text-text-primary' : undefined}>
                g{g.generation}
                {g.is_current ? ' (hiện tại)' : ''}
              </span>
              <span>
                {g.run?.status ?? (g.open_workflow ? 'đang chạy' : '-')}
                {g.decision ? ` · ${g.decision.decision === 'returned' ? 'đã trả về' : 'đã duyệt'}` : ''}
                {g.render ? ` · render ${g.render.state}` : ''}
                {g.publication ? ` · ${g.publication.state}` : ''} · {formatUsd(g.llm_cost_usd)}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Lịch sử thao tác" className="card">
        <h2 className="card-title mb-2">Lịch sử thao tác</h2>
        {(d.dashboard_actions ?? []).length === 0 ? (
          <p className="text-sm text-text-secondary">Chưa có thao tác nào.</p>
        ) : (
          <ul className="divide-y divide-border-subtle/60 text-xs text-text-secondary">
            {d.dashboard_actions!.map((a) => (
              <li key={a.id} className="flex justify-between gap-2 py-1.5">
                <span>{HISTORY_LABELS[a.action] ?? a.action}</span>
                <span>{formatDateTime(a.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
        </div>
      </div>
    </>
  );
}
