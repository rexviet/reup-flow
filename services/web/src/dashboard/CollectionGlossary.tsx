import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { deleteGlossaryTerm, getGlossary, putGlossaryTerm } from '../api/client';
import type { GlossaryTerm } from '../api/types';
import { useConfirm } from './ConfirmDialog';
import { fieldErrorsOf } from './format';

const TERM_FIELDS = ['term_zh', 'term_vi', 'note'] as const;
type TermField = (typeof TERM_FIELDS)[number];
type Errors = Partial<Record<TermField | '', string>>;

/** The PUT body: a blank note is null, as the server stores it. */
function bodyOf(termVi: string, note: string): { term_vi: string; note: string | null } {
  return { term_vi: termVi, note: note.trim() === '' ? null : note };
}

/** The key as the server stores it (stripped, NFC), so a near-duplicate is still recognised as the same term. */
const keyOf = (termZh: string): string => termZh.trim().normalize('NFC');

function matches(term: GlossaryTerm, filter: string): boolean {
  const needle = filter.trim().toLocaleLowerCase('vi');
  if (!needle) return true;
  return [term.term_zh, term.term_vi, term.note ?? ''].some((v) => v.toLocaleLowerCase('vi').includes(needle));
}

/** A field's error, shown right under it and read with it by a screen reader. */
function FieldError({ id, text }: { id: string; text?: string }) {
  return text ? (
    <p id={id} className="mt-0.5 text-xs text-status-error-text">
      {text}
    </p>
  ) : null;
}

/**
 * A collection's glossary: every term with where it came from, a client-side filter, add, edit in
 * place and delete. The server runs the same term rules as the Localizer (`check_term`) and names
 * the field a rule fails on; that message is shown beside that field. A term's key is its Chinese
 * text, so an edit changes the Vietnamese and the note; a different Chinese text is a new term.
 */
export function CollectionGlossary({ collectionId }: { collectionId: string }) {
  const [terms, setTerms] = useState<GlossaryTerm[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [newZh, setNewZh] = useState('');
  const [newVi, setNewVi] = useState('');
  const [newNote, setNewNote] = useState('');
  const [addErrors, setAddErrors] = useState<Errors>({});
  const [editing, setEditing] = useState<{ termZh: string; vi: string; note: string; errors: Errors } | null>(null);
  const [busy, setBusy] = useState(false);
  const { ask, dialog } = useConfirm();

  const load = useCallback(async () => {
    try {
      const res = await getGlossary(collectionId);
      setTerms(res.items);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [collectionId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    if (newZh.trim() === '' || newVi.trim() === '') return;
    const existing = terms?.find((t) => keyOf(t.term_zh) === keyOf(newZh));
    if (existing && !(await ask(`«${existing.term_zh}» đã có (${existing.term_vi}). Ghi đè?`))) return;
    setBusy(true);
    setAddErrors({});
    try {
      await putGlossaryTerm(collectionId, newZh, bodyOf(newVi, newNote));
      setNewZh('');
      setNewVi('');
      setNewNote('');
      await load();
    } catch (err) {
      setAddErrors(fieldErrorsOf(err, TERM_FIELDS));
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveEdit() {
    if (!editing) return;
    setBusy(true);
    try {
      await putGlossaryTerm(collectionId, editing.termZh, bodyOf(editing.vi, editing.note));
      setEditing(null);
      await load();
    } catch (err) {
      const errors = fieldErrorsOf(err, TERM_FIELDS);
      setEditing((prev) => (prev ? { ...prev, errors } : prev));
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(term: GlossaryTerm) {
    if (!(await ask(`Xóa thuật ngữ «${term.term_zh}» → «${term.term_vi}»?`))) return;
    setBusy(true);
    setLoadError(null);
    try {
      await deleteGlossaryTerm(collectionId, term.term_zh);
      await load();
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const shown = (terms ?? []).filter((t) => matches(t, filter));

  return (
    <div className="flex flex-col gap-3">
      <form onSubmit={(e) => void handleAdd(e)} className="grid gap-2 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-start">
        <div>
          <label htmlFor="glossary-new-zh" className="sr-only">
            中文 mới
          </label>
          <input
            id="glossary-new-zh"
            type="text"
            value={newZh}
            onChange={(e) => setNewZh(e.target.value)}
            placeholder="中文"
            aria-describedby={addErrors.term_zh ? 'glossary-new-zh-error' : undefined}
            className="field"
          />
          <FieldError id="glossary-new-zh-error" text={addErrors.term_zh} />
        </div>
        <div>
          <label htmlFor="glossary-new-vi" className="sr-only">
            Tiếng Việt mới
          </label>
          <input
            id="glossary-new-vi"
            type="text"
            value={newVi}
            onChange={(e) => setNewVi(e.target.value)}
            placeholder="Tiếng Việt"
            aria-describedby={addErrors.term_vi ? 'glossary-new-vi-error' : undefined}
            className="field"
          />
          <FieldError id="glossary-new-vi-error" text={addErrors.term_vi} />
        </div>
        <div>
          <label htmlFor="glossary-new-note" className="sr-only">
            Ghi chú mới
          </label>
          <input
            id="glossary-new-note"
            type="text"
            value={newNote}
            onChange={(e) => setNewNote(e.target.value)}
            placeholder="Ghi chú (tùy chọn)"
            aria-describedby={addErrors.note ? 'glossary-new-note-error' : undefined}
            className="field"
          />
          <FieldError id="glossary-new-note-error" text={addErrors.note} />
        </div>
        <button type="submit" disabled={busy || newZh.trim() === '' || newVi.trim() === ''} className="btn btn-neutral">
          Thêm thuật ngữ
        </button>
        {addErrors[''] ? (
          <p role="alert" className="text-xs text-status-error-text sm:col-span-4">
            {addErrors['']}
          </p>
        ) : null}
      </form>

      <div>
        <label htmlFor="glossary-filter" className="sr-only">
          Lọc thuật ngữ
        </label>
        <input id="glossary-filter" type="search" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Lọc thuật ngữ" className="field" />
      </div>

      {loadError ? (
        <p role="alert" className="text-xs text-status-error-text">
          {loadError}
        </p>
      ) : null}

      {terms === null ? (
        <p className="text-sm text-text-secondary">{loadError ? 'Không tải được glossary.' : 'Đang tải…'}</p>
      ) : (
        <div className="overflow-x-auto rounded bg-surface-layer-1">
          <table className="w-full min-w-[40rem] border-collapse text-left text-sm">
            <thead>
              <tr className="bg-surface-container-low font-mono text-[11px] uppercase tracking-wider text-text-muted">
                <th scope="col" className="p-2 font-semibold">中文</th>
                <th scope="col" className="p-2 font-semibold">Tiếng Việt</th>
                <th scope="col" className="p-2 font-semibold">Ghi chú</th>
                <th scope="col" className="p-2 font-semibold">Nguồn</th>
                <th scope="col" className="p-2 text-right font-semibold">Thao tác</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border-subtle/60">
              {shown.length === 0 ? (
                <tr>
                  <td colSpan={5} className="p-3 text-text-secondary">
                    {terms.length === 0 ? 'Chưa có thuật ngữ nào.' : 'Không có thuật ngữ nào khớp.'}
                  </td>
                </tr>
              ) : (
                shown.map((t) => {
                  const edit = editing?.termZh === t.term_zh ? editing : null;
                  const key = encodeURIComponent(t.term_zh);
                  return (
                    <tr key={t.term_zh} data-testid="glossary-row" className="align-top">
                      <td className="p-2 text-text-primary">
                        {t.term_zh}
                        {/* The server re-checks the key on every save; an old agent key can fail it. */}
                        {edit ? <FieldError id={`glossary-zh-${key}-error`} text={edit.errors.term_zh} /> : null}
                      </td>
                      {edit ? (
                        <>
                          <td className="p-2">
                            <label htmlFor={`glossary-vi-${key}`} className="sr-only">
                              Tiếng Việt của {t.term_zh}
                            </label>
                            <input
                              id={`glossary-vi-${key}`}
                              type="text"
                              value={edit.vi}
                              onChange={(e) => setEditing({ ...edit, vi: e.target.value })}
                              aria-describedby={edit.errors.term_vi ? `glossary-vi-${key}-error` : undefined}
                              className="field"
                            />
                            <FieldError id={`glossary-vi-${key}-error`} text={edit.errors.term_vi} />
                          </td>
                          <td className="p-2">
                            <label htmlFor={`glossary-note-${key}`} className="sr-only">
                              Ghi chú của {t.term_zh}
                            </label>
                            <input
                              id={`glossary-note-${key}`}
                              type="text"
                              value={edit.note}
                              onChange={(e) => setEditing({ ...edit, note: e.target.value })}
                              aria-describedby={edit.errors.note ? `glossary-note-${key}-error` : undefined}
                              className="field"
                            />
                            <FieldError id={`glossary-note-${key}-error`} text={edit.errors.note} />
                          </td>
                        </>
                      ) : (
                        <>
                          <td className="p-2 text-text-primary">{t.term_vi}</td>
                          <td className="p-2 text-xs text-text-secondary">{t.note ?? ''}</td>
                        </>
                      )}
                      <td className="p-2 text-xs text-text-secondary">
                        {t.source === 'dashboard' ? (
                          'Tự đặt'
                        ) : (
                          <>
                            Agent
                            {t.source_aweme_id ? (
                              <>
                                {' · '}
                                <a href={`#/video/${t.source_aweme_id}`} className="font-mono text-secondary hover:underline">
                                  {t.source_aweme_id}
                                </a>
                              </>
                            ) : null}
                          </>
                        )}
                      </td>
                      <td className="p-2 text-right">
                        {edit ? (
                          <div className="flex flex-col items-end gap-1">
                            <div className="flex justify-end gap-1">
                              <button type="button" disabled={busy} aria-label={`Lưu ${t.term_zh}`} onClick={() => void handleSaveEdit()} className="btn btn-primary">
                                Lưu
                              </button>
                              <button type="button" onClick={() => setEditing(null)} className="btn btn-ghost">
                                Hủy
                              </button>
                            </div>
                            {edit.errors[''] ? (
                              <p role="alert" className="text-xs text-status-error-text">
                                {edit.errors['']}
                              </p>
                            ) : null}
                          </div>
                        ) : (
                          <div className="flex justify-end gap-1">
                            <button
                              type="button"
                              disabled={busy}
                              aria-label={`Sửa ${t.term_zh}`}
                              onClick={() => setEditing({ termZh: t.term_zh, vi: t.term_vi, note: t.note ?? '', errors: {} })}
                              className="btn btn-ghost"
                            >
                              Sửa
                            </button>
                            <button type="button" disabled={busy} aria-label={`Xóa ${t.term_zh}`} onClick={() => void handleDelete(t)} className="btn btn-ghost">
                              Xóa
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}
      {dialog}
    </div>
  );
}
