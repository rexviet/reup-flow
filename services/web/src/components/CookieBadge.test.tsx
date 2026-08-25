import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { CookieBadge, cookieBadgeState } from './CookieBadge';
import type { CookieStatus } from '../api/types';

describe('cookieBadgeState', () => {
  it('is red when the cookie is not present -- ĐỎ khi không có, §8.3', () => {
    const status: CookieStatus = { present: false, age_seconds: null, last_ok_at: null };
    expect(cookieBadgeState(status)).toBe('red');
  });

  it('is green when present and pushed less than 30 minutes ago', () => {
    const status: CookieStatus = { present: true, age_seconds: 29 * 60, last_ok_at: null };
    expect(cookieBadgeState(status)).toBe('green');
  });

  it('is amber when present but pushed 30 minutes ago or more', () => {
    const status: CookieStatus = { present: true, age_seconds: 30 * 60, last_ok_at: null };
    expect(cookieBadgeState(status)).toBe('amber');
  });

  it('is amber, not green, when age is unknown (present but age_seconds null)', () => {
    const status: CookieStatus = { present: true, age_seconds: null, last_ok_at: null };
    expect(cookieBadgeState(status)).toBe('amber');
  });
});

describe('CookieBadge', () => {
  it('renders the red state with a visible label -- not hidden in a menu (§8.3)', () => {
    render(<CookieBadge status={{ present: false, age_seconds: null, last_ok_at: null }} />);
    const badge = screen.getByTestId('cookie-badge');
    expect(badge).toHaveAttribute('data-state', 'red');
    expect(badge).toBeVisible();
  });

  it('renders a loading state before the first fetch resolves', () => {
    render(<CookieBadge status={null} loading />);
    expect(screen.getByTestId('cookie-badge')).toHaveAttribute('data-state', 'loading');
  });
});
