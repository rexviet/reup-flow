import { useEffect, useId, useRef, type ReactNode } from 'react';
import { Icon } from './Icon';

export interface ModalProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
}

/**
 * A centred dialog over a dimmed page, wherever the page is scrolled. The browser's own modal
 * `<dialog>` traps focus and closes on Escape; a click on the backdrop closes it too.
 */
export function Modal({ title, onClose, children }: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // jsdom has no showModal; the open attribute shows the same content there.
    if (typeof dialog.showModal === 'function') {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
    }
    return () => {
      if (typeof dialog.close === 'function' && dialog.open) dialog.close();
    };
  }, []);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      // Escape: the browser would close the dialog itself; the owner unmounts it instead.
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      // The box has no padding of its own, so a click that lands on the element itself is the backdrop.
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="m-auto max-h-[90vh] w-[min(56rem,calc(100vw-2rem))] overflow-hidden rounded-lg bg-surface-container-low p-0 text-text-primary shadow-xl backdrop:bg-black/60"
    >
      <div className="flex max-h-[90vh] flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-2">
          {/* A title with no space in it (a channel id) still wraps instead of widening the box. */}
          <h3 id={titleId} className="min-w-0 break-all text-sm font-semibold text-text-primary">
            {title}
          </h3>
          <button type="button" onClick={onClose} aria-label="Đóng" className="inline-flex min-h-8 min-w-8 items-center justify-center rounded text-text-muted hover:text-text-primary">
            <Icon name="close" size={14} />
          </button>
        </div>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto overflow-x-hidden">{children}</div>
      </div>
    </dialog>
  );
}
