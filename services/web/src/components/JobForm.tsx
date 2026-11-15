import { useState, type FormEvent } from 'react';
import type { CollectionItem } from '../api/types';
import { COLLECTION_KIND_LABELS } from '../dashboard/format';
import { Icon } from './Icon';

export interface JobFormOptions {
  /** Channel URL: localize every video of this channel as it finishes downloading. */
  autoLocalize: boolean;
  /** Single-video URL: localize this video as soon as it is downloaded instead of holding it. */
  localizeNow: boolean;
  /** A collection picked by hand; absent for "Tự nhận", which lets the server choose. */
  collectionId?: string;
}

export interface JobFormProps {
  onSubmit: (url: string, options: JobFormOptions) => Promise<void>;
  collections?: CollectionItem[];
}

/** True for a URL that names one video (`/video/<id>` or `?modal_id=<id>`); the rest are channel URLs. */
export function isVideoUrl(url: string): boolean {
  return /\/video\/\d+/.test(url) || /[?&]modal_id=\d+/.test(url);
}

/**
 * §8.1: "Ô input URL profile + nút 'Download all'". §8.5: deliberately no
 * cookie paste field here or anywhere else in this package -- see
 * phase-08-web-ui.md §8.5.
 */
export function JobForm({ onSubmit, collections = [] }: JobFormProps) {
  const [url, setUrl] = useState('');
  const [collectionId, setCollectionId] = useState('');
  const [autoLocalize, setAutoLocalize] = useState(false);
  const [localizeNow, setLocalizeNow] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;

    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(trimmed, { autoLocalize, localizeNow, ...(collectionId ? { collectionId } : {}) });
      setUrl('');
      setLocalizeNow(false);
      // A picked collection is a one-off: intake never moves a hand-placed video back.
      setCollectionId('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 sm:flex-row sm:items-start">
      <div className="flex-1">
        <label htmlFor="job-url" className="sr-only">
          URL profile
        </label>
        <input
          id="job-url"
          type="text"
          placeholder="https://www.douyin.com/user/..."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          className="field"
        />
        <div className="mt-1 flex flex-wrap gap-x-4">
          {isVideoUrl(url) ? (
            <label className="inline-flex min-h-11 items-center gap-2 text-xs text-text-secondary">
              <input type="checkbox" className="h-5 w-5" checked={localizeNow} onChange={(e) => setLocalizeNow(e.target.checked)} />
              Localize ngay
            </label>
          ) : (
            <label className="inline-flex min-h-11 items-center gap-2 text-xs text-text-secondary">
              <input type="checkbox" className="h-5 w-5" checked={autoLocalize} onChange={(e) => setAutoLocalize(e.target.checked)} />
              Tự localize
            </label>
          )}
        </div>
        <div className="mt-1 flex items-center gap-2">
          <label htmlFor="job-collection" className="shrink-0 text-xs text-text-secondary">
            Collection
          </label>
          <select id="job-collection" value={collectionId} onChange={(e) => setCollectionId(e.target.value)} className="field">
            <option value="">Tự nhận (mặc định)</option>
            {collections.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({COLLECTION_KIND_LABELS[c.kind] ?? c.kind})
              </option>
            ))}
          </select>
        </div>
        {error ? (
          <p className="mt-1 text-xs text-status-error-text" data-testid="job-form-error">
            {error}
          </p>
        ) : null}
      </div>
      <button
        type="submit"
        disabled={submitting}
        className="btn btn-primary text-sm"
      >
        <Icon name="download" />
        {submitting ? 'Đang tạo…' : 'Download all'}
      </button>
    </form>
  );
}
