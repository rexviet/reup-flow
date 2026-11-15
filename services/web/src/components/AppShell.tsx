import type { ReactNode } from 'react';
import { LIST_POLL_MS, HEALTH_POLL_MS } from '../dashboard/useDashboard';
import { Icon, type IconName } from './Icon';

export interface NavEntry {
  href: string;
  label: string;
  icon: IconName;
  current: boolean;
}

/** One entry per real route: the dashboard and, while open, the video being looked at. */
export function navEntries(awemeId: string | null): NavEntry[] {
  return [
    { href: '#/', label: 'Bảng điều khiển', icon: 'dashboard', current: awemeId === null },
    ...(awemeId ? [{ href: `#/video/${awemeId}`, label: `Video ${awemeId}`, icon: 'video' as const, current: true }] : []),
  ];
}

function NavLinks({ entries, orientation }: { entries: NavEntry[]; orientation: 'vertical' | 'horizontal' }) {
  return (
    <ul className={orientation === 'vertical' ? 'flex flex-col gap-0.5' : 'flex gap-1 overflow-x-auto p-1'}>
      {entries.map((e) => (
        <li key={e.href} className="min-w-0">
          <a
            href={e.href}
            aria-current={e.current ? 'page' : undefined}
            className={`flex min-h-11 items-center gap-2 rounded px-3 py-2 text-sm transition-colors ${
              e.current
                ? 'bg-surface-container-highest font-medium text-primary'
                : 'text-on-surface-variant hover:bg-surface-container hover:text-on-surface'
            }`}
          >
            <Icon name={e.icon} size={18} />
            <span className="truncate">{e.label}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}

/** Top bar everywhere, a left sidebar from `lg` up and the same links under the top bar below it. */
export function AppShell({ awemeId, children }: { awemeId: string | null; children: ReactNode }) {
  const entries = navEntries(awemeId);
  return (
    <div className="min-h-screen bg-surface text-on-surface">
      <header className="fixed inset-x-0 top-0 z-30 flex h-14 items-center justify-between gap-3 bg-surface-container-low/95 px-4 shadow-[0_1px_8px_rgba(0,0,0,0.4)] backdrop-blur-md">
        <div className="flex min-w-0 items-center gap-3">
          <span className="truncate text-base font-semibold tracking-tight text-text-primary">Douyin Pipeline Console</span>
        </div>
        <p className="hidden shrink-0 items-center gap-3 font-mono text-xs text-text-muted sm:flex">
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-secondary" />
            Bảng: {LIST_POLL_MS / 1000}s
          </span>
          <span aria-hidden="true">•</span>
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-secondary" />
            Sức khỏe: {HEALTH_POLL_MS / 1000}s
          </span>
        </p>
      </header>

      <aside className="fixed bottom-0 left-0 top-14 z-20 hidden w-56 flex-col gap-2 bg-surface-layer-1 p-2 shadow-[1px_0_12px_rgba(0,0,0,0.3)] lg:flex">
        <nav aria-label="Điều hướng chính" className="flex flex-col gap-1">
          <div className="eyebrow px-3 py-1">Điều phối pipeline</div>
          <NavLinks entries={entries} orientation="vertical" />
        </nav>
      </aside>

      <div className="pt-14 lg:pl-56">
        <nav aria-label="Điều hướng nhanh" className="border-b border-border-subtle bg-surface-layer-1 px-2 py-1 lg:hidden">
          <NavLinks entries={entries} orientation="horizontal" />
        </nav>
        <main className="mx-auto w-full max-w-[1600px] p-3 sm:p-4">{children}</main>
      </div>
    </div>
  );
}
