import { useState, type FormEvent } from 'react';
import { Icon } from './Icon';

export interface JobFormOptions {
  /** Channel URL: localize every video of this channel as it finishes downloading. */
  autoLocalize: boolean;
  /** Single-video URL: localize this video as soon as it is downloaded instead of holding it. */
  localizeNow: boolean;
}

export interface JobFormProps {
  onSubmit: (url: string, options: JobFormOptions) => Promise<void>;
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
export function JobForm({ onSubmit }: JobFormProps) {
  const [url, setUrl] = useState('');
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
      await onSubmit(trimmed, { autoLocalize, localizeNow });
      setUrl('');
      setLocalizeNow(false);
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
