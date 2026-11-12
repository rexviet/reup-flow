import { useEffect, useState } from 'react';
import { getMediaLinks } from '../api/client';
import type { MediaKind, MediaLink } from '../api/types';

/** Renew a link this long before it expires, and never more often than the floor. */
const RENEW_BEFORE_MS = 60_000;
const MIN_RENEW_MS = 5_000;
const RETRY_MS = 15_000;

/** Signed media URLs for one generation, renewed before they expire. `error` is set while a fetch is failing. */
export function useMediaLinks(awemeId: string, generation: number | null, kinds: MediaKind[]) {
  const [links, setLinks] = useState<Partial<Record<MediaKind, MediaLink>>>({});
  const [error, setError] = useState<string | null>(null);
  const kindsKey = kinds.join(',');

  useEffect(() => {
    if (generation === null || kindsKey === '') return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function load() {
      let delay = RETRY_MS;
      try {
        const fetched = await getMediaLinks(awemeId, generation as number, kindsKey.split(',') as MediaKind[]);
        if (cancelled) return;
        setLinks(Object.fromEntries(fetched.map((l) => [l.kind, l])));
        setError(null);
        const expiries = fetched.flatMap((l) => (l.expires_at ? [Date.parse(l.expires_at)] : []));
        if (expiries.length > 0) delay = Math.max(MIN_RENEW_MS, Math.min(...expiries) - Date.now() - RENEW_BEFORE_MS);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      }
      if (!cancelled) timer = setTimeout(() => void load(), delay);
    }
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [awemeId, generation, kindsKey]);

  return { links, error };
}
