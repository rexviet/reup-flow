import { z } from 'zod';

/**
 * Value rules for a collection's settings. They match the CHECK constraints in
 * migrations/261114-collections.sql (and later collection migrations), so a bad value is a 400 here and never a
 * constraint violation (500) in Postgres.
 */

const codePoints = (text: string): number => [...text].length;
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/u;
/** A lone surrogate would be stored as U+FFFD, not as the text that was checked. */
const LONE_SURROGATE = /\p{Cs}/u;

/** `#rrggbb` in either case, stored lower case: the edit-spec `HexColor` only accepts lower case. */
export const hexColorSchema = z
  .string()
  .regex(/^#[0-9a-fA-F]{6}$/, 'must be a colour written #rrggbb')
  .transform((v) => v.toLowerCase());

/** Background-music offset in dB: −24 to 24 in steps of 0.5. */
export const bgmOffsetDbSchema = z
  .number()
  .min(-24)
  .max(24)
  .refine((v) => Number.isInteger(v * 2), 'must be a multiple of 0.5');

/** Opacity of the caption box as a whole percentage, like `Cover.opacity_pct`. */
export const coverOpacityPctSchema = z.number().int().min(0).max(100);

/**
 * Where the caption box sits, as whole percentages of the frame
 * (migrations/261114-collection-cover-geometry.sql): the shift of its centre
 * (positive = down), its width (centred) and its height.
 */
export const coverOffsetPctSchema = z.number().int().min(-50).max(50);
export const coverWidthPctSchema = z.number().int().min(20).max(100);
export const coverHeightPctSchema = z.number().int().min(3).max(50);

/**
 * A folder relative to REVIEW_WORKER_EXPORT_ROOT on the Mac. Every segment must be
 * a real name: no leading or trailing `/`, no empty, `.` or `..` segment, no NUL or
 * other control character, at most 512 characters. The Mac worker checks the
 * resolved path again before it writes; this keeps nonsense out of the table.
 */
export const saveSubdirSchema = z
  .string()
  .refine((v) => codePoints(v) >= 1 && codePoints(v) <= 512, 'must be 1 to 512 characters')
  .refine((v) => !CONTROL.test(v), 'must not contain control characters')
  .refine((v) => !LONE_SURROGATE.test(v), 'must not contain a lone surrogate')
  .refine(
    (v) => v.split('/').every((segment) => segment !== '' && segment !== '.' && segment !== '..'),
    'must be a relative path whose every segment is a name (no leading "/", no empty, "." or ".." segment)',
  );

const nameSchema = z
  .string()
  .max(1000)
  .transform((v) => v.trim())
  .refine((v) => codePoints(v) >= 1 && codePoints(v) <= 200, 'must be 1 to 200 characters')
  .refine((v) => !CONTROL.test(v), 'must not contain control characters')
  .refine((v) => !LONE_SURROGATE.test(v), 'must not contain a lone surrogate');

/** Each setting may be left out; `null` means "use the shared default". */
const settingsShape = {
  bgm_offset_db: bgmOffsetDbSchema.nullable().optional(),
  caption_text_color: hexColorSchema.nullable().optional(),
  cover_color: hexColorSchema.nullable().optional(),
  cover_opacity_pct: coverOpacityPctSchema.nullable().optional(),
  cover_offset_pct: coverOffsetPctSchema.nullable().optional(),
  cover_width_pct: coverWidthPctSchema.nullable().optional(),
  cover_height_pct: coverHeightPctSchema.nullable().optional(),
  save_subdir: saveSubdirSchema.nullable().optional(),
};

export const SETTING_KEYS = Object.keys(settingsShape) as Array<keyof typeof settingsShape>;

/** `POST /api/collections`: always a `manual` collection. */
export const createCollectionSchema = z
  .object({ name: nameSchema, sec_user_id: z.string().min(1).max(200).optional(), ...settingsShape })
  .strict();
export type CreateCollectionBody = z.infer<typeof createCollectionSchema>;

/** `PATCH /api/collections/:id`: the name and any setting; kind and owner never change. */
export const updateCollectionSchema = z
  .object({ name: nameSchema.optional(), ...settingsShape })
  .strict()
  .refine((v) => Object.keys(v).length > 0, 'nothing to change');
export type UpdateCollectionBody = z.infer<typeof updateCollectionSchema>;

export const collectionParamsSchema = z.object({ id: z.string().uuid() }).strict();

export const glossaryParamsSchema = z
  .object({ id: z.string().uuid(), term_zh: z.string().min(1).max(1000) })
  .strict();

export const glossaryBodySchema = z
  .object({ term_vi: z.string().max(1000), note: z.string().max(2000).nullish() })
  .strict();
export type GlossaryBody = z.infer<typeof glossaryBodySchema>;
