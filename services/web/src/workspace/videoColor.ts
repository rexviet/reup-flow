/** One colour per video, so two videos moving at once can be told apart. */
const PALETTE = ['#2563eb', '#db2777', '#d97706', '#059669', '#7c3aed', '#dc2626', '#0891b2', '#65a30d'];

export function videoColor(awemeId: string | undefined): string {
  if (!awemeId) return '#6b7280';
  let h = 0;
  for (const c of awemeId) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export const shortId = (awemeId: string): string => awemeId.slice(-4);
