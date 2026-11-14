/**
 * Where the subtitle cover box sits in a review project, from the frame size, the source subtitle
 * band and a collection's three geometry settings. The localization worker draws the box with the
 * same rules (localization `layout_captions`); the collection dialog uses this to preview it while
 * the numbers are typed.
 */

export interface CoverGeometryInput {
  frameWidth: number;
  frameHeight: number;
  /** The detected source subtitle band in pixels (only `y` and `h` matter), or null when none was found. */
  region: { y: number; h: number } | null;
  /** Shift of the box centre, % of frame height, positive = down; null = automatic. */
  offsetPct: number | null;
  /** Box width, % of frame width, centred; null = automatic. */
  widthPct: number | null;
  /** Box height, % of frame height; null = automatic. */
  heightPct: number | null;
}

export interface CoverBox {
  /** Top edge in pixels of the frame. */
  top: number;
  /** Height in pixels of the frame. */
  height: number;
  /** Width as a % of the frame width; the box is centred horizontally. */
  widthPct: number;
}

/** Without a detected band, the box sits at 80 % of the frame height and is 14 % of it tall. */
const FALLBACK_TOP = 0.8;
const FALLBACK_HEIGHT = 0.14;
/** The width of a box nobody sized: 60 % of the frame. */
const BASE_WIDTH_PCT = 60;

/**
 * Python's `round`: halves go to the even neighbour. The worker places the fallback box with it,
 * so the preview does too.
 */
export function roundHalfEven(x: number): number {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** Half-up, as the worker rounds every value the collection settings introduce (`_half_up`). */
const halfUp = (x: number): number => Math.floor(x + 0.5);

/**
 * The review-path box: base from the band (or the fallback), then height, offset, clamp and width.
 * With nothing set the base box comes back untouched, as the worker leaves it.
 */
export function coverBox({ frameHeight: H, region, offsetPct, widthPct, heightPct }: CoverGeometryInput): CoverBox {
  const baseTop = region ? region.y : roundHalfEven(H * FALLBACK_TOP);
  const baseHeight = region ? region.h : roundHalfEven(H * FALLBACK_HEIGHT);
  if (offsetPct === null && widthPct === null && heightPct === null) {
    return { top: baseTop, height: baseHeight, widthPct: BASE_WIDTH_PCT };
  }
  const height = heightPct === null ? baseHeight : Math.max(1, Math.min(H, halfUp((H * heightPct) / 100)));
  const centre = baseTop + baseHeight / 2 + halfUp((H * (offsetPct ?? 0)) / 100);
  const top = Math.max(0, Math.min(halfUp(centre - height / 2), H - height));
  return { top, height, widthPct: widthPct ?? BASE_WIDTH_PCT };
}
