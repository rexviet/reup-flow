'use strict';

const { formatLastPushResult } = require('../lib/options-view');

describe('formatLastPushResult', () => {
  test('null (never pushed) renders a distinct "never pushed" state', () => {
    expect(formatLastPushResult(null)).toEqual({
      statusLabel: 'never pushed yet',
      timeLabel: '',
      messageLabel: '',
    });
  });

  test('a successful push renders OK with the timestamp and message', () => {
    const result = formatLastPushResult({
      at: '2026-08-25T09:41:00.000Z',
      ok: true,
      skipped: false,
      message: 'pushed successfully',
    });
    expect(result.statusLabel).toBe('OK');
    expect(result.messageLabel).toBe('pushed successfully');
    expect(result.timeLabel).not.toBe('');
  });

  // DoD: "Sai API key -> options page hiện lỗi rõ" -- the wrong-key message
  // from lib/api-client.js must survive unmangled and be labeled ERROR, so
  // this is distinguishable at a glance from "extension chết" (phase-09
  // §9.4's whole reason for existing).
  test('a wrong-API-key failure renders ERROR with the specific 401 message intact', () => {
    const result = formatLastPushResult({
      at: '2026-08-25T09:41:00.000Z',
      ok: false,
      skipped: false,
      message: 'backend rejected the API key (401 Unauthorized)',
    });
    expect(result.statusLabel).toBe('ERROR');
    expect(result.messageLabel).toBe('backend rejected the API key (401 Unauthorized)');
    expect(result.messageLabel).toMatch(/API key/);
  });

  test('a skipped push (empty cookie list) renders SKIPPED, not ERROR', () => {
    const result = formatLastPushResult({
      at: '2026-08-25T09:41:00.000Z',
      ok: false,
      skipped: true,
      message: 'no douyin cookies found (logged out?) -- push skipped, existing backend cookie left untouched',
    });
    expect(result.statusLabel).toBe('SKIPPED');
    expect(result.messageLabel).toMatch(/logged out/);
  });
});
