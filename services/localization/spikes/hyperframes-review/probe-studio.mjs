// Read-only boundary probe: an HTTP 200 is evidence, not an exploit claim.
const base = 'http://127.0.0.1:18440';
const results = [];
for (const [name, headers] of [
  ['normal', {}], ['bad-host', { Host: 'evil.test' }],
  ['bad-origin', { Origin: 'https://evil.test' }],
]) {
  const response = await fetch(`${base}/api/projects`, { headers });
  results.push({ name, status: response.status,
    accessControlAllowOrigin: response.headers.get('access-control-allow-origin') });
  await response.arrayBuffer();
}
console.log(JSON.stringify(results, null, 2));
