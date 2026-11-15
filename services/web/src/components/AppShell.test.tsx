import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AppShell, navEntries } from './AppShell';

describe('AppShell', () => {
  it('lists only real routes: the dashboard, plus the open video while on its page', () => {
    expect(navEntries(null)).toEqual([expect.objectContaining({ href: '#/', current: true })]);
    const onVideo = navEntries('7001');
    expect(onVideo.map((e) => e.href)).toEqual(['#/', '#/video/7001']);
    expect(onVideo.map((e) => e.current)).toEqual([false, true]);
  });

  it('marks the current page for assistive tech and renders the page content', () => {
    render(
      <AppShell awemeId="7001">
        <p>nội dung</p>
      </AppShell>,
    );
    expect(screen.getByText('nội dung')).toBeInTheDocument();
    const current = screen.getAllByRole('link', { name: 'Video 7001', hidden: true });
    for (const link of current) expect(link).toHaveAttribute('aria-current', 'page');
    for (const link of screen.getAllByRole('link', { name: 'Bảng điều khiển', hidden: true })) expect(link).not.toHaveAttribute('aria-current');
  });
});
