import { useState, type FormEvent } from 'react';

export interface JobFormProps {
  onSubmit: (url: string) => Promise<void>;
}

/**
 * §8.1: "Ô input URL profile + nút 'Download all'". §8.5: deliberately no
 * cookie paste field here or anywhere else in this package -- see
 * phase-08-web-ui.md §8.5.
 */
export function JobForm({ onSubmit }: JobFormProps) {
  const [url, setUrl] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;

    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(trimmed);
      setUrl('');
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
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
        />
        {error ? (
          <p className="mt-1 text-xs text-red-700" data-testid="job-form-error">
            {error}
          </p>
        ) : null}
      </div>
      <button
        type="submit"
        disabled={submitting}
        className="whitespace-nowrap rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {submitting ? 'Đang tạo…' : 'Download all'}
      </button>
    </form>
  );
}
