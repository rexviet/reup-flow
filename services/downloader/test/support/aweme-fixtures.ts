/**
 * Real Douyin payloads, captured from the running parser fork on 2026-08-26.
 *
 * WHY THESE ARE FILES ON DISK AND NOT A LITERAL IN A SPEC. The guard they exist
 * for (`aweme.mapper.ts`'s image-post detection) reads the SHAPE of Douyin's
 * response, and plan §5 rule 7 is the standing lesson about that: a fixture
 * easier than reality is its own kind of false green. Phase 4 shipped a
 * `greatest()` on `max_cursor` that was wrong in DIRECTION, and no test in the
 * repo could see it because every hand-written fake returned ascending cursors.
 * A hand-written "image post" would have exactly that problem -- it would carry
 * the fields whoever wrote it already believed were the distinguishing ones.
 *
 * So each file is one complete `fetch_one_video` envelope, byte-for-byte as the
 * parser returned it, and the item under test is `.data.aweme_detail`. Nothing
 * is trimmed, reordered or scrubbed: the two image posts keep all 148 top-level
 * keys and the video keeps all 146, including the fields that make the
 * distinction LOOK easy (`aweme_type`, `media_type`, the CDN hostname,
 * `duration: 0`) and are deliberately NOT what the mapper keys on.
 *
 * The four items, and how they differ where it matters:
 *
 *   image post 7575023855045328251  aweme_type 68  media_type 2  images[5]
 *                                   duration 0     play_addr -> lf26-music-east
 *                                                                .douyinstatic.com/....mp3
 *   image post 7518711366992743739  aweme_type 68  media_type 2  images[5]
 *                                   duration 0     play_addr -> sf6-cdn-tos
 *                                                                .douyinstatic.com/....mp3
 *   image post 7389167757058772251  aweme_type 68  media_type 2  images[6]
 *                                   duration 0     play_addr -> sf11-cdn-tos
 *                                                                .douyinstatic.com/obj/
 *                                                                tos-cn-ve-2774/... (NO .mp3,
 *                                                                no "music" in the host)
 *   video      7481199234537033018  aweme_type 55  media_type 4  images: null
 *                                   duration 16950 play_addr -> v26-webf.douyinvod.com/...
 *
 * THE THIRD IMAGE POST IS THE IMPORTANT ONE, and it was found by auditing the
 * archive rather than by reading the report. The run that produced this bug was
 * described as "2 of 215 files are MP3s", because `file(1)` says
 * "MPEG ADTS, layer III" for exactly two of them. A byte-level sweep of all 215
 * says SIX: two begin `ID3`, and four begin `ftypM4A` -- audio in an MP4
 * container, which `file(1)` reports as MP4 and which no eye would catch in a
 * listing. All six were then confirmed against the parser: `aweme_type 68`,
 * `media_type 2`, `images` of 5-9 entries. Every one is an image post.
 *
 * That is what this fixture pins. Four of the six carry a `play_addr` with no
 * `.mp3` in it and no `music` in the hostname, so a guard keyed on the URL --
 * the first thing anybody reaches for -- would have fixed 2 of 6 and reported
 * success. `images` catches all six.
 *
 * The video is the control, and a control in the strict sense: any change that
 * stops it being downloadable is a worse bug than the one being fixed.
 *
 * SECRETS: none. Checked with gitleaks against the repo config -- the only
 * findings were 14 hits on Douyin's public `url_key` CDN cache field, which has
 * an audited, canary-verified exemption in `.gitleaks.toml`. There is no cookie
 * or token belonging to us in these files; the `x-signature` query parameters
 * are Douyin's own short-lived CDN URL signatures and expire on their own.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Aweme } from '../../src/parser/parser.client';

const DIR = join(__dirname, '..', 'fixtures', 'aweme');

/**
 * The 图文 posts that were saved as audio named `.mp4`. The third one's
 * `play_addr` is not an `.mp3` and not on a `music` host -- see the note above.
 */
export const IMAGE_POST_FIXTURES = [
  'image-post-7575023855045328251.json',
  'image-post-7518711366992743739.json',
  'image-post-7389167757058772251.json',
] as const;

/** The must-still-work control: a real video from the same run. */
export const VIDEO_FIXTURE = 'video-7481199234537033018.json';

export const IMAGE_POST_IDS = [
  '7575023855045328251',
  '7518711366992743739',
  '7389167757058772251',
] as const;
export const VIDEO_ID = '7481199234537033018';

/**
 * The `.data.aweme_detail` item out of one captured envelope.
 *
 * Read and parsed on every call rather than cached, so a test that mutates an
 * item cannot leak that mutation into the next test -- these objects are 148
 * keys deep and a shared reference would be a very quiet way to make the second
 * assertion in a file measure the first one's edits.
 *
 * The envelope is walked rather than assumed: a fixture that stopped containing
 * `data.aweme_detail` (a re-capture against a changed parser, say) must fail
 * LOUDLY here, not hand back `undefined` for every test to treat as an item with
 * no fields -- which the mapper would correctly drop, turning a broken fixture
 * into a suite that still looks like it is asserting something.
 */
export function awemeFixture(file: string): Aweme {
  const raw = readFileSync(join(DIR, file), 'utf8');
  const envelope: unknown = JSON.parse(raw);
  const data = (envelope as { data?: { aweme_detail?: unknown } }).data;
  const detail = data?.aweme_detail;
  if (typeof detail !== 'object' || detail === null || Array.isArray(detail)) {
    throw new Error(
      `fixture ${file} has no object at .data.aweme_detail -- it is not a fetch_one_video ` +
        `envelope, and every assertion made against it would be vacuous`,
    );
  }
  return detail as Aweme;
}

/** `n` = 0, 1 or 2; see IMAGE_POST_FIXTURES. */
export function imagePostAweme(n: 0 | 1 | 2): Aweme {
  return awemeFixture(IMAGE_POST_FIXTURES[n]);
}

export function videoAweme(): Aweme {
  return awemeFixture(VIDEO_FIXTURE);
}
