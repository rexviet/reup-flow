import {
  createCollectionSchema,
  saveSubdirSchema,
  updateCollectionSchema,
} from '../../src/collections/collections.schemas';

const ok = (v: unknown) => createCollectionSchema.safeParse({ name: 'Phim', ...(v as object) });

describe('collection setting validation', () => {
  describe('colours', () => {
    it('stores an upper-case colour in lower case, as the edit-spec HexColor requires', () => {
      const parsed = createCollectionSchema.parse({ name: 'A', caption_text_color: '#FFFF00', cover_color: '#AbCdEf' });
      expect(parsed.caption_text_color).toBe('#ffff00');
      expect(parsed.cover_color).toBe('#abcdef');
    });

    it.each(['ffff00', '#fff', '#ffff000', '#gggggg', ' #ffff00', '#ffff00\n', 'red'])('rejects %j', (color) => {
      expect(ok({ caption_text_color: color }).success).toBe(false);
      expect(ok({ cover_color: color }).success).toBe(false);
    });
  });

  describe('bgm_offset_db', () => {
    it.each([-24, -23.5, 0, 0.5, 12, 24])('accepts %p', (v) => {
      expect(ok({ bgm_offset_db: v }).success).toBe(true);
    });
    it.each([-24.5, 24.5, 0.25, 1.1, Number.NaN, '3'])('rejects %p', (v) => {
      expect(ok({ bgm_offset_db: v }).success).toBe(false);
    });
  });

  describe('cover_opacity_pct', () => {
    it.each([0, 1, 55, 100])('accepts %p', (v) => {
      expect(ok({ cover_opacity_pct: v }).success).toBe(true);
    });
    it.each([-1, 101, 0.5, 55.5, '50'])('rejects %p', (v) => {
      expect(ok({ cover_opacity_pct: v }).success).toBe(false);
    });
  });

  describe('save_subdir', () => {
    it.each(['Phim', 'Phim/成名的代价', 'a/b/c', '.hidden', 'a..b', 'x'.repeat(512), '字'.repeat(512)])(
      'accepts %j',
      (v) => {
        expect(saveSubdirSchema.safeParse(v).success).toBe(true);
      },
    );
    it.each([
      ['empty', ''],
      ['absolute', '/Users/me/Movies'],
      ['trailing slash', 'Phim/'],
      ['empty segment', 'a//b'],
      ['dot segment', 'a/./b'],
      ['lone dot', '.'],
      ['parent segment', '../escape'],
      ['parent in the middle', 'a/../../b'],
      ['NUL', 'a\u0000b'],
      ['line break', 'a\nb'],
      ['too long', 'x'.repeat(513)],
      ['too many characters, counted as code points', '😀'.repeat(513)],
      ['a lone surrogate', 'Phim/\ud800'],
    ])('rejects %s', (_label, v) => {
      expect(saveSubdirSchema.safeParse(v).success).toBe(false);
    });
  });

  describe('create', () => {
    it('needs a name and takes an optional channel', () => {
      expect(createCollectionSchema.safeParse({}).success).toBe(false);
      expect(createCollectionSchema.safeParse({ name: '   ' }).success).toBe(false);
      expect(createCollectionSchema.safeParse({ name: 'x'.repeat(201) }).success).toBe(false);
      expect(createCollectionSchema.safeParse({ name: 'Phim \udc00' }).success).toBe(false);
      expect(createCollectionSchema.parse({ name: '  Phim hay ', sec_user_id: 'MS4w' })).toEqual({
        name: 'Phim hay',
        sec_user_id: 'MS4w',
      });
    });

    it('rejects unknown keys, kind and id included', () => {
      expect(ok({ kind: 'mix' }).success).toBe(false);
      expect(ok({ id: '00000000-0000-0000-0000-000000000000' }).success).toBe(false);
    });
  });

  describe('update', () => {
    it('takes null for a setting, which means back to the shared default', () => {
      expect(updateCollectionSchema.parse({ caption_text_color: null, save_subdir: null })).toEqual({
        caption_text_color: null,
        save_subdir: null,
      });
    });

    it('refuses an empty patch, a null name and unknown keys', () => {
      expect(updateCollectionSchema.safeParse({}).success).toBe(false);
      expect(updateCollectionSchema.safeParse({ name: null }).success).toBe(false);
      expect(updateCollectionSchema.safeParse({ sec_user_id: 'x' }).success).toBe(false);
    });

    it('applies the same value rules as create', () => {
      expect(updateCollectionSchema.parse({ cover_color: '#00FF00' })).toEqual({ cover_color: '#00ff00' });
      expect(updateCollectionSchema.safeParse({ save_subdir: '../x' }).success).toBe(false);
      expect(updateCollectionSchema.safeParse({ bgm_offset_db: 0.3 }).success).toBe(false);
    });
  });
});
