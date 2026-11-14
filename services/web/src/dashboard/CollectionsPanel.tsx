import { useState, type FormEvent, type ReactNode } from 'react';
import { createCollection, updateCollection } from '../api/client';
import type { CollectionItem, UpdateCollectionBody } from '../api/types';
import { Modal } from '../components/Modal';
import { CollectionGlossary } from './CollectionGlossary';
import { CoverPreview } from './CoverPreview';
import { COLLECTION_KIND_LABELS, fieldErrorsOf, SAVE_ROOT_LABEL } from './format';

const HEX = /^#[0-9a-fA-F]{6}$/;
const SETTING_FIELDS = [
  'name',
  'bgm_offset_db',
  'caption_text_color',
  'cover_color',
  'cover_opacity_pct',
  'cover_offset_pct',
  'cover_width_pct',
  'cover_height_pct',
  'save_subdir',
] as const;
type SettingField = (typeof SETTING_FIELDS)[number];
type Errors = Partial<Record<SettingField | '', string>>;
/** The cover box geometry fields: whole percentages within the server's ranges (collections.schemas.ts). */
const GEOMETRY = {
  cover_offset_pct: { min: -50, max: 50, label: 'Dời hộp nền theo chiều dọc (%)', hint: 'Từ −50 đến 50, số dương dời xuống. Để trống: tự động theo phụ đề gốc.' },
  cover_width_pct: { min: 20, max: 100, label: 'Chiều rộng hộp nền (%)', hint: 'Từ 20 đến 100, tính theo chiều rộng khung. Để trống: tự động theo phụ đề gốc.' },
  cover_height_pct: { min: 3, max: 50, label: 'Chiều cao hộp nền (%)', hint: 'Từ 3 đến 50, tính theo chiều cao khung. Để trống: tự động theo phụ đề gốc.' },
} as const;
type GeometryField = keyof typeof GEOMETRY;
const GEOMETRY_FIELDS = Object.keys(GEOMETRY) as GeometryField[];

/** A geometry field as typed: null when empty (automatic), else a whole number in range, else the error to show. */
function parseGeometry(field: GeometryField, raw: string): { value: number | null; error?: string } {
  const text = raw.trim();
  if (text === '') return { value: null };
  const { min, max } = GEOMETRY[field];
  const value = Number(text);
  if (!Number.isInteger(value) || value < min || value > max) {
    return { value: null, error: `Phải là số nguyên từ ${String(min).replace('-', '−')} đến ${max}.` };
  }
  return { value };
}

/** Where the opacity slider starts when a collection stops using the shared value; the admin then moves it. */
const OPACITY_START = 100;

export interface CollectionsPanelProps {
  /** `null` until the first answer arrives. */
  collections: CollectionItem[] | null;
  /** The last failed load; the list keeps its last good answer meanwhile. */
  error?: Error | null;
  onChanged: () => void;
}

/** Every collection with its kind, size and folder; create one by name, edit its settings and glossary. */
export function CollectionsPanel({ collections, error = null, onChanged }: CollectionsPanelProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const current = collections?.find((c) => c.id === editing) ?? null;

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    setCreateError(null);
    try {
      const made = await createCollection({ name });
      setNewName('');
      setEditing(made.id);
      onChanged();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  return (
    <section aria-labelledby="collections-title" className="card flex flex-col gap-3">
      <h2 id="collections-title" className="card-title">
        Collections
      </h2>

      <form onSubmit={(e) => void handleCreate(e)} className="flex flex-col gap-2 sm:flex-row">
        <label htmlFor="new-collection" className="sr-only">
          Tên collection mới
        </label>
        <input
          id="new-collection"
          type="text"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="Tên collection mới"
          maxLength={200}
          className="field flex-1"
        />
        <button type="submit" disabled={creating || newName.trim() === ''} className="btn btn-neutral">
          Tạo collection
        </button>
      </form>
      {createError ? (
        <p role="alert" className="text-xs text-status-error-text">
          {createError}
        </p>
      ) : null}

      {error ? (
        <p role="alert" className="text-xs text-status-warning-text">
          Không tải được collection: {error.message}
        </p>
      ) : null}
      {collections === null ? (
        error ? null : <p className="text-sm text-text-secondary">Đang tải…</p>
      ) : collections.length === 0 ? (
        <p className="text-sm text-text-secondary">Chưa có collection nào.</p>
      ) : (
        <ul className="divide-y divide-border-subtle/60">
          {collections.map((c) => (
            <li key={c.id} data-testid="collection-row" className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <div className="min-w-0">
                <p className="break-all font-medium text-text-primary">
                  {c.name}{' '}
                  <span className="rounded bg-surface-layer-2 px-1 font-mono text-[11px] text-text-muted">{COLLECTION_KIND_LABELS[c.kind] ?? c.kind}</span>
                </p>
                <p className="font-mono text-xs text-text-muted">
                  {c.video_count} video · {c.save_subdir === null ? 'Chưa đặt thư mục' : `${SAVE_ROOT_LABEL}/${c.save_subdir}`}
                </p>
              </div>
              <button
                type="button"
                aria-label={`Sửa ${c.name}`}
                aria-haspopup="dialog"
                onClick={() => setEditing(c.id)}
                className="btn btn-ghost"
              >
                Sửa
              </button>
            </li>
          ))}
        </ul>
      )}

      {current ? <CollectionEditor key={current.id} collection={current} onSaved={onChanged} onClose={() => setEditing(null)} /> : null}
    </section>
  );
}

/** Opens over the page where the admin is, so a long list never pushes it out of sight. */
function CollectionEditor({ collection, onSaved, onClose }: { collection: CollectionItem; onSaved: () => void; onClose: () => void }) {
  const [tab, setTab] = useState<'settings' | 'glossary'>('settings');
  const tabClass = (on: boolean) => `btn ${on ? 'btn-neutral' : 'btn-ghost'}`;
  return (
    <Modal title={`Sửa «${collection.name}»`} onClose={onClose}>
      <div role="tablist" aria-label="Phần cần sửa" className="flex gap-1.5">
        <button type="button" role="tab" id="tab-settings" aria-selected={tab === 'settings'} aria-controls="panel-collection" onClick={() => setTab('settings')} className={tabClass(tab === 'settings')}>
          Setting
        </button>
        <button type="button" role="tab" id="tab-glossary" aria-selected={tab === 'glossary'} aria-controls="panel-collection" onClick={() => setTab('glossary')} className={tabClass(tab === 'glossary')}>
          Glossary
        </button>
      </div>
      <div role="tabpanel" id="panel-collection" aria-labelledby={tab === 'settings' ? 'tab-settings' : 'tab-glossary'}>
        {tab === 'settings' ? <SettingsForm collection={collection} onSaved={onSaved} /> : <CollectionGlossary collectionId={collection.id} />}
      </div>
    </Modal>
  );
}

/** One labelled control with its error beside it, tied to the control so a screen reader reads both. */
function Field({ id, label, error, hint, children }: { id: string; label: string; error?: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className="text-xs text-text-secondary">
        {label}
      </label>
      {children}
      {hint ? <p className="text-[11px] text-text-muted">{hint}</p> : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-status-error-text">
          {error}
        </p>
      ) : null}
    </div>
  );
}

const describedBy = (id: string, error?: string) => (error ? `${id}-error` : undefined);

function ColorField({ id, label, value, onChange, error }: { id: string; label: string; value: string; onChange: (v: string) => void; error?: string }) {
  const valid = HEX.test(value.trim());
  return (
    <Field id={id} label={label} error={error} hint="Để trống: dùng màu chung.">
      <div className="flex items-center gap-2">
        <span
          data-testid={`swatch-${id.replace('collection-', '')}`}
          aria-hidden="true"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded ring-1 ring-border-prominent"
          style={valid ? { backgroundColor: value.trim() } : undefined}
        >
          {valid ? null : <span className="text-[10px] text-text-muted">chung</span>}
        </span>
        <input
          id={id}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="#rrggbb"
          maxLength={7}
          aria-describedby={describedBy(id, error)}
          aria-invalid={error ? true : undefined}
          className="field font-mono"
        />
      </div>
    </Field>
  );
}

function SettingsForm({ collection, onSaved }: { collection: CollectionItem; onSaved: () => void }) {
  const [name, setName] = useState(collection.name);
  const [bgm, setBgm] = useState(collection.bgm_offset_db === null ? '' : String(collection.bgm_offset_db));
  const [captionColor, setCaptionColor] = useState(collection.caption_text_color ?? '');
  const [coverColor, setCoverColor] = useState(collection.cover_color ?? '');
  const [opacity, setOpacity] = useState<number | null>(collection.cover_opacity_pct);
  const [saveSubdir, setSaveSubdir] = useState(collection.save_subdir ?? '');
  const [geometry, setGeometry] = useState<Record<GeometryField, string>>(() => ({
    cover_offset_pct: collection.cover_offset_pct === null ? '' : String(collection.cover_offset_pct),
    cover_width_pct: collection.cover_width_pct === null ? '' : String(collection.cover_width_pct),
    cover_height_pct: collection.cover_height_pct === null ? '' : String(collection.cover_height_pct),
  }));
  const parsed = Object.fromEntries(GEOMETRY_FIELDS.map((f) => [f, parseGeometry(f, geometry[f])])) as Record<GeometryField, { value: number | null; error?: string }>;
  const [errors, setErrors] = useState<Errors>({});
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  /** The PATCH body, or the errors that keep it from being sent. Empty means the shared default (null). */
  function bodyOf(): { body?: UpdateCollectionBody; errors: Errors } {
    const found: Errors = {};
    const color = (field: 'caption_text_color' | 'cover_color', raw: string): string | null => {
      const v = raw.trim();
      if (v === '') return null;
      if (!HEX.test(v)) found[field] = 'Màu phải viết dạng #rrggbb, ví dụ #ffff00.';
      return v;
    };
    const trimmedName = name.trim();
    if (trimmedName === '') found.name = 'Tên không được để trống.';
    const bgmText = bgm.trim();
    const bgmValue = bgmText === '' ? null : Number(bgmText);
    const subdir = saveSubdir.trim();
    if (bgmValue !== null && !Number.isFinite(bgmValue)) found.bgm_offset_db = 'Phải là một số dB.';
    for (const field of GEOMETRY_FIELDS) {
      const error = parsed[field].error;
      if (error) found[field] = error;
    }
    const body: UpdateCollectionBody = {
      name: trimmedName,
      bgm_offset_db: bgmValue,
      caption_text_color: color('caption_text_color', captionColor),
      cover_color: color('cover_color', coverColor),
      cover_opacity_pct: opacity,
      cover_offset_pct: parsed.cover_offset_pct.value,
      cover_width_pct: parsed.cover_width_pct.value,
      cover_height_pct: parsed.cover_height_pct.value,
      save_subdir: subdir === '' ? null : subdir,
    };
    if (Object.keys(found).length > 0) return { errors: found };
    // Only what changed, so an edit made elsewhere since this form opened is not reverted.
    const changed: UpdateCollectionBody = {};
    for (const key of SETTING_FIELDS) {
      if (body[key] !== collection[key]) Object.assign(changed, { [key]: body[key] });
    }
    return { body: changed, errors: {} };
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    setNotice(null);
    const { body, errors: found } = bodyOf();
    setErrors(found);
    if (!body) return;
    if (Object.keys(body).length === 0) {
      setNotice('Không có thay đổi.');
      return;
    }
    setSaving(true);
    try {
      await updateCollection(collection.id, body);
      setNotice('Đã lưu.');
      onSaved();
    } catch (err) {
      setErrors(fieldErrorsOf(err, SETTING_FIELDS));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={(e) => void handleSave(e)} className="grid gap-3 sm:grid-cols-2">
      <Field id="collection-name" label="Tên collection" error={errors.name}>
        <input
          id="collection-name"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={200}
          aria-describedby={describedBy('collection-name', errors.name)}
          className="field"
        />
      </Field>
      <Field id="collection-bgm" label="Độ lệch nhạc nền (dB)" error={errors.bgm_offset_db} hint="Từ −24 đến 24, bước 0,5. Để trống: dùng mức chung.">
        <input
          id="collection-bgm"
          type="number"
          step={0.5}
          min={-24}
          max={24}
          value={bgm}
          onChange={(e) => setBgm(e.target.value)}
          aria-describedby={describedBy('collection-bgm', errors.bgm_offset_db)}
          className="field font-mono"
        />
      </Field>
      <ColorField id="collection-caption_text_color" label="Màu chữ" value={captionColor} onChange={setCaptionColor} error={errors.caption_text_color} />
      <ColorField id="collection-cover_color" label="Màu hộp nền" value={coverColor} onChange={setCoverColor} error={errors.cover_color} />
      <Field id="collection-opacity" label="Độ mờ hộp nền" error={errors.cover_opacity_pct}>
        <div className="flex items-center gap-2">
          <input
            id="collection-opacity"
            type="range"
            min={0}
            max={100}
            step={1}
            value={opacity ?? OPACITY_START}
            disabled={opacity === null}
            onChange={(e) => setOpacity(Math.round(Number(e.target.value)))}
            aria-describedby={describedBy('collection-opacity', errors.cover_opacity_pct)}
            className="flex-1"
          />
          <span className="w-12 text-right font-mono text-xs text-text-primary">{opacity === null ? 'chung' : `${opacity}%`}</span>
        </div>
        <label className="inline-flex min-h-9 items-center gap-2 text-xs text-text-secondary">
          <input
            type="checkbox"
            className="h-5 w-5"
            checked={opacity === null}
            onChange={(e) => setOpacity(e.target.checked ? null : OPACITY_START)}
          />
          Dùng độ mờ chung
        </label>
      </Field>
      <Field id="collection-save_subdir" label="Thư mục lưu" error={errors.save_subdir} hint="Thư mục con, ví dụ phim/ten-phim. Để trống: chưa lưu được.">
        <div className="flex items-center gap-1">
          <span data-testid="save-root-prefix" className="shrink-0 rounded bg-surface-layer-2 px-1.5 py-1 font-mono text-[11px] text-text-muted">
            {SAVE_ROOT_LABEL}/
          </span>
          <input
            id="collection-save_subdir"
            type="text"
            value={saveSubdir}
            onChange={(e) => setSaveSubdir(e.target.value)}
            maxLength={512}
            aria-describedby={describedBy('collection-save_subdir', errors.save_subdir)}
            className="field font-mono"
          />
        </div>
      </Field>
      {/* A fieldset is min-content wide by default; min-w-0 lets it shrink to the dialog. */}
      <fieldset className="flex min-w-0 flex-col gap-3 sm:col-span-2 md:flex-row">
        <legend className="mb-1 text-xs font-medium text-text-secondary">Vị trí hộp nền</legend>
        {/* Beside the fields on a wide dialog, so the box can be watched while typing. */}
        <div className="min-w-0 md:w-3/5">
          <CoverPreview
            collectionId={collection.id}
            offsetPct={parsed.cover_offset_pct.value}
            widthPct={parsed.cover_width_pct.value}
            heightPct={parsed.cover_height_pct.value}
            color={HEX.test(coverColor.trim()) ? coverColor.trim().toLowerCase() : null}
            opacityPct={opacity}
          />
        </div>
        <div className="grid min-w-0 gap-3 sm:grid-cols-3 md:flex-1 md:grid-cols-1 md:content-start">
          {GEOMETRY_FIELDS.map((field) => {
            const id = `collection-${field}`;
            const { label, hint } = GEOMETRY[field];
            return (
              <Field key={field} id={id} label={label} error={errors[field]} hint={hint}>
                <input
                  id={id}
                  type="number"
                  // No native min/max/step: the browser would block the submit with its own bubble
                  // instead of the message beside the field.
                  step="any"
                  value={geometry[field]}
                  onChange={(e) => setGeometry((g) => ({ ...g, [field]: e.target.value }))}
                  placeholder="tự động"
                  aria-describedby={describedBy(id, errors[field])}
                  aria-invalid={errors[field] ? true : undefined}
                  className="field font-mono"
                />
              </Field>
            );
          })}
        </div>
      </fieldset>
      <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
        <button type="submit" disabled={saving} className="btn btn-primary">
          {saving ? 'Đang lưu…' : 'Lưu setting'}
        </button>
        {notice ? (
          <span role="status" className="text-xs text-status-success-text">
            {notice}
          </span>
        ) : null}
        {errors[''] ? (
          <span role="alert" className="text-xs text-status-error-text">
            {errors['']}
          </span>
        ) : null}
      </div>
    </form>
  );
}
