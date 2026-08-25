'use strict';

const { originPatternFor, ensureBackendHostPermission } = require('../lib/backend-permission');

describe('originPatternFor', () => {
  test('builds a host-permission match pattern from an http URL', () => {
    expect(originPatternFor('http://localhost:3000')).toBe('http://localhost:3000/*');
  });

  test('builds a host-permission match pattern from an https URL, ignoring path/trailing slash', () => {
    expect(originPatternFor('https://downloader.internal/some/path')).toBe('https://downloader.internal/*');
    expect(originPatternFor('http://localhost:3000/')).toBe('http://localhost:3000/*');
  });

  test('returns null for a non-http(s) scheme', () => {
    expect(originPatternFor('ftp://example.com')).toBeNull();
    expect(originPatternFor('javascript:alert(1)')).toBeNull();
  });

  test('returns null for unparseable input', () => {
    expect(originPatternFor('not a url')).toBeNull();
    expect(originPatternFor('')).toBeNull();
  });
});

describe('ensureBackendHostPermission', () => {
  test('resolves granted=true without prompting when the permission already exists', async () => {
    const calls = { contains: [], request: [] };
    const permissionsApi = {
      contains(details, cb) {
        calls.contains.push(details);
        cb(true);
      },
      request(details, cb) {
        calls.request.push(details);
        cb(true);
      },
    };

    const result = await ensureBackendHostPermission(permissionsApi, 'http://localhost:3000');

    expect(result).toEqual({ granted: true, pattern: 'http://localhost:3000/*' });
    expect(calls.contains).toHaveLength(1);
    expect(calls.request).toHaveLength(0); // never prompts if already granted
  });

  test('prompts via permissions.request when not already granted, and reports the outcome', async () => {
    const permissionsApi = {
      contains(_details, cb) {
        cb(false);
      },
      request(_details, cb) {
        cb(true);
      },
    };

    const result = await ensureBackendHostPermission(permissionsApi, 'http://localhost:3000');
    expect(result.granted).toBe(true);
  });

  test('reports granted=false with a reason when the user denies the prompt', async () => {
    const permissionsApi = {
      contains(_details, cb) {
        cb(false);
      },
      request(_details, cb) {
        cb(false);
      },
    };

    const result = await ensureBackendHostPermission(permissionsApi, 'http://localhost:3000');
    expect(result.granted).toBe(false);
    expect(result.reason).toBeTruthy();
  });

  test('reports granted=false without calling the permissions API for an invalid backend URL', async () => {
    const calls = { contains: 0, request: 0 };
    const permissionsApi = {
      contains(_d, cb) { calls.contains += 1; cb(false); },
      request(_d, cb) { calls.request += 1; cb(false); },
    };

    const result = await ensureBackendHostPermission(permissionsApi, 'not a url');
    expect(result.granted).toBe(false);
    expect(result.pattern).toBeNull();
    expect(calls.contains).toBe(0);
    expect(calls.request).toBe(0);
  });
});
