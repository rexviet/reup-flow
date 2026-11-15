import { useEffect, useRef, useState } from 'react';
import { getMediaLinks, getThumbLinks } from '../api/client';
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

/** The server signs at most this many links per request. */
const THUMB_BATCH = 100;

/**
 * Thumbnail URLs of the videos on screen, by aweme id. Links are asked for only for ids without a
 * fresh one, so the list's 3-second poll asks for nothing and a page change asks once; each is
 * renewed shortly before it expires. The server keeps a URL the same for its whole 10-minute
 * window, so a renewed one within it is a browser cache hit. An id without a URL yet has none in
 * the result; a URL whose image is missing fails to load and the caller shows a placeholder.
 */
export function useThumbLinks(awemeIds: readonly string[]): Readonly<Record<string, string>> {
  // The effect reads the ref (it outlives renders); rendering reads the state copy of it.
  const cache = useRef(new Map<string, { url: string; expiresAt: number }>());
  const [urls, setUrls] = useState<Readonly<Record<string, string>>>({});
  const idsKey = [...new Set(awemeIds)].join(',');

  useEffect(() => {
    if (idsKey === '') return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ids = idsKey.split(',');
    const fresh = (id: string) => (cache.current.get(id)?.expiresAt ?? 0) - Date.now() > RENEW_BEFORE_MS;

    async function load() {
      let delay = RETRY_MS;
      try {
        const stale = ids.filter((id) => !fresh(id));
        for (let i = 0; i < stale.length; i += THUMB_BATCH) {
          const fetched = await getThumbLinks(stale.slice(i, i + THUMB_BATCH));
          if (cancelled) return;
          for (const l of fetched) cache.current.set(l.aweme_id, { url: l.url, expiresAt: Date.parse(l.expires_at) });
        }
        if (stale.length > 0) setUrls(Object.fromEntries([...cache.current].map(([id, l]) => [id, l.url])));
        const expiries = ids.flatMap((id) => {
          const at = cache.current.get(id)?.expiresAt;
          return at === undefined || Number.isNaN(at) ? [] : [at];
        });
        if (expiries.length === ids.length) delay = Math.max(MIN_RENEW_MS, Math.min(...expiries) - Date.now() - RENEW_BEFORE_MS);
      } catch {
        // Thumbnails are decoration: the rows keep their placeholder and the next try comes later.
      }
      if (!cancelled) timer = setTimeout(() => void load(), delay);
    }
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [idsKey]);

  return urls;
}
