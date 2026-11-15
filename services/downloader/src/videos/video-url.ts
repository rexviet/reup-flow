/**
 * The `aweme_id` of the single video a Douyin page URL points at, or `null`.
 *
 * Two page shapes carry one: `/video/<id>` (also under `iesdouyin.com/share`),
 * and any page that opens the video in a modal over another page
 * (`?modal_id=<id>` -- the home feed, a profile, search). A short link
 * (`v.douyin.com/...`) is NOT resolved here: that needs an outbound redirect,
 * and this runs before the host-allowlisted URL reaches the parser. `/note/`
 * is an image post, not a video, so it is not matched either.
 */
const VIDEO_PATH = /^\/(?:share\/)?video\/(\d+)\/?$/;
const NUMERIC_ID = /^\d+$/;

export function extractAwemeId(rawUrl: string): string | null {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    return null;
  }
  const fromPath = VIDEO_PATH.exec(url.pathname);
  if (fromPath) return fromPath[1];
  const modalId = url.searchParams.get('modal_id');
  return modalId !== null && NUMERIC_ID.test(modalId) ? modalId : null;
}
