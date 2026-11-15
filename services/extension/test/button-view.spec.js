'use strict';

const { describeEnqueueResult } = require('../lib/button-view');

describe('describeEnqueueResult', () => {
  test.each([
    ['pending', 'ok'],
    ['downloading', 'ok'],
    ['done', 'ok'],
    ['failed', 'error'],
    ['skipped', 'error'],
  ])('queued with backend status %s is shown as %s', (status, kind) => {
    const view = describeEnqueueResult({ ok: true, status, message: 'queued' });
    expect(view.kind).toBe(kind);
    expect(view.text.length).toBeGreaterThan(0);
  });

  test('a failure shows the message it carries', () => {
    const view = describeEnqueueResult({ ok: false, message: 'backend rejected the API key (401 Unauthorized)' });
    expect(view).toEqual({ kind: 'error', text: 'backend rejected the API key (401 Unauthorized)' });
  });

  test('no reply at all is an error, not silence', () => {
    expect(describeEnqueueResult(undefined).kind).toBe('error');
  });

  test('each status reads differently, so a click on a finished video is not mistaken for a new one', () => {
    const texts = ['pending', 'downloading', 'done', 'failed', 'skipped'].map(
      (status) => describeEnqueueResult({ ok: true, status }).text,
    );
    expect(new Set(texts).size).toBe(texts.length);
  });
});
