// No response mocking: real intake, SQLAdmin, CSRF POST and SMTP worker.
import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { chromium } from '@playwright/test';

const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const read = async () => JSON.parse((await lines.next()).value);
const exchange = async (action, rest = {}) => {
  process.stdout.write(JSON.stringify({ action, ...rest }) + '\n');
  const result = await read();
  assert.equal(result.result, 'PASS');
  return result;
};
const config = await read();
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined });
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: false });
  const catalog = await (await context.request.get(`${config.origin}/api/v1/catalog/items`)).json();
  const selected = ['DEMO-P-001', 'DEMO-S-001'].map(sku => catalog.items.find(item => item.sku === sku));
  assert(selected.every(Boolean));
  const consent = await (await context.request.get(`${config.origin}/api/v1/consent-text`)).json();
  const headers = { 'Idempotency-Key': randomUUID(), 'X-Request-Token': randomBytes(32).toString('hex') };
  const data = {
    kind: 'commercial', source: 'site', confirmed: true,
    items: selected.map(item => ({ item_id: item.id, qty: 2, expected_revision: item.revision })),
    contact: { name: 'Synthetic Retry Contact', email: 'retry@example.invalid' },
    comment: 'Synthetic notification retry acceptance',
    consent: { version: consent.version, accepted: true },
  };
  const created = await context.request.post(`${config.origin}/api/v1/orders`, { headers, data });
  assert.equal(created.status(), 201);
  const receipt = await created.json();
  const failed = await exchange('fail');
  const page = await context.newPage();
  await page.goto(`${config.origin}/admin/login`);
  await page.getByLabel('Login', { exact: true }).fill('browser-manager');
  await page.getByLabel('Password', { exact: true }).fill(config.passwords.manager);
  const [loggedIn] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/admin/auth/login'),
    page.getByRole('button', { name: 'Log in', exact: true }).click(),
  ]);
  assert.equal(loggedIn.status(), 303);
  await page.waitForURL(`${config.origin}/admin/`);
  const url = `${config.origin}/admin/orders/${failed.order_id}`;
  const initial = await page.goto(url);
  assert.equal(initial.status(), 200);
  assert.equal(initial.headers()['cache-control'], 'no-store');
  assert((await initial.securityDetails()).protocol.startsWith('TLS'));
  const notification = page.locator('section[aria-labelledby=notification]');
  assert((await notification.innerText()).includes('failed'));
  const form = notification.locator('form');
  assert.equal(await form.getAttribute('method'), 'post');
  const csrf = await form.locator('input[name=csrf_token]').inputValue();
  assert(csrf);
  const [retried] = await Promise.all([
    page.waitForResponse(response => response.url() === url + '/retry-notification'),
    page.getByRole('button', { name: 'Повторить уведомление', exact: true }).click(),
  ]);
  assert.equal(retried.status(), 303);
  assert.equal(retried.headers().location, `/admin/orders/${failed.order_id}`);
  await page.waitForURL(url);
  await page.waitForLoadState();
  assert((await notification.innerText()).includes('pending'));
  assert.equal(await notification.getByRole('button').count(), 0);
  await exchange('retried'); // PG verifies pending before worker is released
  await exchange('send'); // existing tick and actual SMTP transport, no mocked send
  await page.reload();
  assert((await notification.innerText()).includes('sent'));
  assert.equal(await page.getByRole('button', { name: 'Повторить уведомление', exact: true }).count(), 0);
  const conflict = await context.request.post(url + '/retry-notification', {
    form: { csrf_token: csrf }, headers: { Origin: config.origin }, maxRedirects: 0,
  });
  assert.equal(conflict.status(), 409);
  assert.equal(conflict.headers()['cache-control'], 'no-store');
  const replay = await context.request.post(`${config.origin}/api/v1/orders`, { headers, data });
  assert.equal(replay.status(), 200);
  assert.deepEqual(await replay.json(), receipt);
  const cookie = (await context.cookies()).find(item => item.name === '__Host-staff_session');
  assert(cookie && cookie.secure && cookie.httpOnly && cookie.sameSite === 'Strict');
  await exchange('done', { proof: {
    result: 'PASS: MVP-018 Chromium/HTTPS SQLAdmin failed → pending → sending → sent; SMTP wire and Message-ID; audit actor; unchanged business rows/outbox identity; replay 200; repeated POST 409',
    secrets: [headers['X-Request-Token'], headers['Idempotency-Key'], csrf, cookie.value],
  } });
  await context.close();
} finally {
  await browser.close();
}
