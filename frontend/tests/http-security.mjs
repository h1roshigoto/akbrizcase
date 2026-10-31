// Trusted TLS with the shipped CSP, real Nginx limits and PostgreSQL quotas.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomBytes, randomUUID } from 'node:crypto';
import { chromium } from '@playwright/test';
const config = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined });
const secrets = [];
const rateError = async (response, maximum) => {
  assert.equal(response.status(), 429);
  const headers = response.headers();
  assert.equal(headers['cache-control'], 'no-store');
  assert(Number(headers['retry-after']) >= 1 && Number(headers['retry-after']) <= maximum);
  const body = await response.json();
  assert.deepEqual(body.error, { code: 'RATE_LIMITED', message: 'Too many requests', details: {} });
  assert.match(body.request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  assert.equal(headers['x-request-id'], body.request_id);
  assert(!JSON.stringify(body).includes('198.51.100'));
};
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: false });
  const page = await context.newPage();
  const root = await page.goto(config.origin);
  assert.equal(root.status(), 200);
  const scriptPolicy = root.headers()['content-security-policy'].split('script-src ')[1].split(';')[0];
  assert.equal(scriptPolicy, "'self'");
  await page.evaluate(() => {
    const script = document.createElement('script');
    script.textContent = 'window.syntheticInlineExecuted=true';
    document.head.appendChild(script);
  });
  assert.equal(await page.evaluate(() => window.syntheticInlineExecuted), undefined);
  for (const src of await page.locator('script[src]').evaluateAll(scripts => scripts.map(s => s.src))) {
    assert.equal(new URL(src).origin, config.origin);
  }
  for (const origin of [config.httpOrigin, config.origin]) {
    for (const path of ['/files', '/files/private', '/documents', '/images/private', '/.secrets/ip_hmac_secret']) {
      assert.equal((await context.request.get(origin + path)).status(), 404);
    }
    const cross = await context.request.post(origin + '/api/v1/chat/sessions', {
      headers: { Origin: 'https://foreign.invalid' }, data: { mode: 'knowledge' },
    });
    assert.equal(cross.status(), 403);
    assert.equal(cross.headers()['access-control-allow-origin'], undefined);
    assert.equal(cross.headers()['cache-control'], 'no-store');
    const preflight = await context.request.fetch(origin + '/api/v1/chat/sessions', {
      method: 'OPTIONS', headers: { Origin: 'https://foreign.invalid', 'Access-Control-Request-Method': 'POST' },
    });
    assert.equal(preflight.status(), 403);
    assert.equal(preflight.headers()['access-control-allow-origin'], undefined);
  }
  let own;
  for (let index = 0; index < 11; index++) {
    const origin = index % 2 ? config.httpOrigin : config.origin;
    const response = await context.request.post(origin + '/api/v1/chat/sessions', {
      headers: { Origin: origin, 'X-Forwarded-For': `198.51.100.${index + 1}`,
        Forwarded: `for=198.51.100.${index + 1};proto=http`,
        'X-Akbriz-Client-IP': `198.51.100.${index + 1}`, 'X-Akbriz-Scheme': 'wrong' },
      data: { mode: 'knowledge' },
    });
    if (index === 10) await rateError(response, 3600);
    else {
      assert.equal(response.status(), 201);
      assert.equal(response.headers()['cache-control'], 'no-store');
      const session = await response.json();
      secrets.push(session.session_token);
      own ??= session;
    }
  }
  const policy = await (await context.request.get(config.origin + '/api/v1/consent-text')).json();
  const body = { kind: 'support', items: [], contact: { name: 'Synthetic MVP039', email: 'mvp039@example.invalid' },
    source: 'site', comment: 'Synthetic independently confirmed issue', confirmed: true,
    consent: { version: policy.version, accepted: true } };
  const token = randomBytes(32).toString('hex');
  secrets.push(token);
  const headers = { 'Idempotency-Key': randomUUID(), 'X-Request-Token': token };
  let receipt;
  for (let index = 0; index < 6; index++) {
    const response = await context.request.post(config.origin + '/api/v1/orders', {
      headers: index === 0 ? headers : { ...headers, 'Idempotency-Key': randomUUID() }, data: body,
    });
    if (index === 5) await rateError(response, 3600);
    else { assert.equal(response.status(), 201); receipt ??= await response.json(); }
  }
  const replay = await context.request.post(config.origin + '/api/v1/orders', { headers, data: body });
  assert.equal(replay.status(), 200);
  assert.deepEqual(await replay.json(), receipt);
  assert.equal((await context.request.post(config.origin + '/api/v1/orders', {
    headers, data: { ...body, comment: 'Synthetic changed' },
  })).status(), 409);
  for (const key of [headers['Idempotency-Key'], randomUUID()]) {
    await rateError(await context.request.post(config.origin + '/api/v1/orders', {
      headers: { ...headers, 'Idempotency-Key': key, 'X-Request-Token': 'b'.repeat(64) }, data: body,
    }), 3600);
  }
  assert.equal((await context.request.post(config.origin + '/api/v1/orders', {
    headers: { ...headers, 'X-Request-Token': 'bad' }, data: body,
  })).status(), 422);
  let message;
  for (let version = 0; version < 31; version++) {
    message = { session_id: own.session_id, request_id: randomUUID(), expected_version: version,
      input: { type: 'select_mode', mode: 'knowledge' } };
    const response = await context.request.post(config.origin + '/api/v1/chat/messages', {
      headers: { 'X-Chat-Token': own.session_token }, data: message,
    });
    if (version === 30) await rateError(response, 60);
    else assert.equal(response.status(), 200);
  }
  for (let index = 0; index < 6; index++) {
    const response = await context.request.post(config.origin + '/admin/auth/login', {
      headers: { Origin: 'https://foreign.invalid', 'X-Forwarded-For': `198.51.100.${index + 1}` }, data: {},
    });
    if (index === 5) await rateError(response, 12);
    else assert.equal(response.status(), 403);
  }
  // Drain the shared HTTP/HTTPS leaky bucket without changing configured rates.
  // Earlier calls already used capacity, so exact initial-burst counting belongs
  // to the isolated boundary test; here both server blocks must enforce it.
  const results = await Promise.all(Array.from({ length: 150 }, (_, index) => context.request.get(
    (index % 2 ? config.httpOrigin : config.origin) + '/api/v1/catalog/items',
    { headers: { 'X-Forwarded-For': `198.51.100.${index + 1}`, Forwarded: 'for=203.0.113.2' } },
  )));
  assert(results.some(r => r.status() === 200));
  assert(results.some(r => r.status() === 429));
  for (const result of results) {
    if (result.status() === 429) await rateError(result, 1);
    else assert.equal(result.status(), 200);
  }
  for (const origin of [config.httpOrigin, config.origin]) {
    const responses = await Promise.all(Array.from({ length: 5 }, () => context.request.get(origin + '/api/v1/catalog/items')));
    assert(responses.some(r => r.status() === 429));
  }
  const mutations = await Promise.all(Array.from({ length: 5 }, () => context.request.post(
    config.origin + '/admin/statics/js/jquery.min.js', { data: {} },
  )));
  assert(mutations.some(r => r.status() === 429), 'Static exemption must never cover mutations');
  await context.close();
  process.stdout.write(JSON.stringify({ secrets, result: 'PASS: MVP-039 trusted HTTPS CSP/XSS, HTTP+HTTPS same-origin/CORS, 10 sessions/hour, 5 new orders/hour, free AQ-01 replay/full-quota masking, 30 messages/minute, 120/minute Nginx API and 5/minute login, spoofed headers, 429/Retry-After/no-store, private storage denied' }));
} finally { await browser.close(); }
