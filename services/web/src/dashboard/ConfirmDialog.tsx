import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

export interface ConfirmDialogProps {
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({ message, confirmLabel = 'Xác nhận', onConfirm, onCancel }: ConfirmDialogProps) {
  useEffect(() => {
    const onKey = (e: { key: string }) => {
      if (e.key === 'Escape') onCancel();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onCancel]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div role="alertdialog" aria-modal="true" aria-label="Xác nhận" aria-describedby="confirm-message" className="w-full max-w-md rounded-xl bg-surface-container-high p-5 shadow-xl ring-1 ring-border-prominent">
        <p id="confirm-message" className="text-sm text-text-primary">{message}</p>
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" autoFocus onClick={onCancel} className="btn btn-ghost text-sm">
            Hủy
          </button>
          <button type="button" onClick={onConfirm} className="btn btn-primary text-sm">
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/** `ask(message)` resolves true or false once the admin answers; render `dialog` somewhere in the tree. */
export function useConfirm(): { ask: (message: string) => Promise<boolean>; dialog: ReactNode } {
  const [message, setMessage] = useState<string | null>(null);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const ask = useCallback((text: string) => {
    resolver.current?.(false);
    setMessage(text);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const settle = (ok: boolean) => {
    resolver.current?.(ok);
    resolver.current = null;
    setMessage(null);
  };

  const dialog =
    message === null ? null : <ConfirmDialog message={message} onConfirm={() => settle(true)} onCancel={() => settle(false)} />;
  return { ask, dialog };
}
