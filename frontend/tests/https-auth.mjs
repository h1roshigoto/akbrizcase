// Real Chromium -> TLS Nginx -> FastAPI -> PostgreSQL. No authentication mocks.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomBytes, randomUUID } from 'node:crypto';
import { chromium } from '@playwright/test';

const config = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
});
const sensitive = [];
try {
  // Use the real public intake API; no SMTP service is configured in Compose.
  const intake = await browser.newContext({ ignoreHTTPSErrors: false });
  const catalogResponse = await intake.request.get(`${config.origin}/api/v1/catalog/items`);
  assert.equal(catalogResponse.status(), 200);
  const catalog = (await catalogResponse.json()).items;
  const selected = ['DEMO-P-001', 'DEMO-S-001'].map(sku => catalog.find(item => item.sku === sku));
  assert(selected.every(Boolean));
  const consentResponse = await intake.request.get(`${config.origin}/api/v1/consent-text`);
  assert.equal(consentResponse.status(), 200);
  const consent = await consentResponse.json();
  const supportDescription = '<script>window.orderInjected=true</script><img src=x onerror="alert(1)"> & " synthetic support';
  const receipts = {};
  for (const kind of ['commercial', 'support']) {
    const token = randomBytes(32).toString('hex');
    sensitive.push(token);
    const response = await intake.request.post(`${config.origin}/api/v1/orders`, {
      headers: { 'Idempotency-Key': randomUUID(), 'X-Request-Token': token },
      data: {
        kind, source: 'site', confirmed: true,
        items: kind === 'support' ? [] : selected.map(item => ({
          item_id: item.id, qty: 2, expected_revision: item.revision,
        })),
        contact: { name: 'Synthetic Browser Contact', email: 'browser@example.invalid' },
        comment: kind === 'support' ? supportDescription : 'Synthetic commercial request',
        consent: { version: consent.version, accepted: true },
      },
    });
    assert.equal(response.status(), 201);
    receipts[kind] = await response.json();
  }
  assert.equal((await intake.request.get(`${config.origin}/admin/orders`)).status(), 401);
  await intake.close();
  for (const role of ['manager', 'admin']) {
    const context = await browser.newContext({ ignoreHTTPSErrors: false });
    const page = await context.newPage();
    let sentCookie = false;
    page.on('request', async request => {
      if (new URL(request.url()).pathname === '/admin/') {
        sentCookie ||= (await request.allHeaders()).cookie?.includes('__Host-staff_session=') ?? false;
      }
    });
    const initial = await page.goto(`${config.origin}/admin/login`);
    assert.equal(initial.status(), 200);
    assert.equal(await page.evaluate(() => window.isSecureContext), true);
    await page.getByLabel('Login', { exact: true }).fill(`browser-${role}`);
    await page.getByLabel('Password', { exact: true }).fill(config.passwords[role]);
    const [loginResponse] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === '/admin/auth/login'),
      page.getByRole('button', { name: 'Log in', exact: true }).click(),
    ]);
    if (loginResponse.status() !== 303) {
      const body = await loginResponse.json();
      assert.fail(`Browser login returned ${loginResponse.status()} ${body.error?.code}`);
    }
    await page.waitForURL(`${config.origin}/admin/`);
    await page.getByRole('button', { name: 'Log out', exact: true }).waitFor();
    const cookies = await context.cookies();
    const cookie = cookies.find(item => item.name === '__Host-staff_session');
    assert(cookie && cookie.secure && cookie.httpOnly && cookie.sameSite === 'Strict');
    assert.equal(cookie.path, '/');
    assert.equal(cookie.domain, 'localhost');
    assert(sentCookie, 'Chromium must send the session cookie to SQLAdmin');
    assert(!(await page.evaluate(() => document.cookie)).includes('__Host-staff_session'));
    sensitive.push(cookie.value);
    const csrf = await page.locator('input[name=csrf_token]').first().inputValue();
    sensitive.push(csrf);
    for (const token of ['', 'forged']) {
      const status = await page.evaluate(async token => (await fetch('/admin/auth/logout', {
        method: 'POST', headers: { 'X-CSRF-Token': token },
      })).status, token);
      assert.equal(status, 403);
    }
    const foreign = await context.request.post(`${config.origin}/admin/auth/logout`, {
      headers: { Origin: 'https://foreign.invalid', 'X-CSRF-Token': csrf },
    });
    assert.equal(foreign.status(), 403);
    const protectedResponse = await page.reload();
    assert.equal(protectedResponse.status(), 200);
    assert.equal(protectedResponse.headers()['cache-control'], 'no-store');
    const tls = await protectedResponse.securityDetails();
    assert(tls.protocol.startsWith('TLS'));
    await page.getByRole('link', { name: 'Orders', exact: true }).click();
    await page.waitForURL(`${config.origin}/admin/orders`);
    assert(!(await page.locator('main').innerText()).includes(supportDescription));
    await page.getByRole('link', { name: String(receipts.commercial.number), exact: true }).click();
    const detailUrl = page.url();
    const orderText = await page.locator('main').innerText();
    assert(orderText.includes(`Order ${receipts.commercial.number}`));
    assert(orderText.includes('Current status / version'));
    assert(orderText.includes('Known subtotal (may be partial)'));
    assert(orderText.includes('Requires quotation'));
    assert(orderText.includes('pending_quote'));
    assert(orderText.includes('pending'));
    assert(orderText.includes(consent.version));
    assert(orderText.includes('Consent evidence') && orderText.includes('request_form'));
    assert(orderText.includes('Status history') && orderText.includes('System'));
    for (const item of selected) {
      assert(orderText.includes(item.sku) && orderText.includes(item.name));
    }
    assert.equal(await page.locator('table').first().locator('tbody tr').count(), 2);
    assert.equal(await page.locator('form').count(), 2); // Logout and narrow status action.
    assert.equal(await page.getByRole('button', { name: 'Change status', exact: true }).count(), 1);
    assert.equal(await page.getByRole('button', { name: /edit|delete|retry/i }).count(), 0);
    const jsonResponse = await context.request.get(`${config.origin}/api/v1/admin/orders`);
    assert.equal(jsonResponse.status(), 200);
    assert.equal(jsonResponse.headers()['cache-control'], 'no-store');
    const jsonPage = await jsonResponse.json();
    assert.equal(jsonPage.total, 2);
    const support = jsonPage.items.find(item => item.number === receipts.support.number);
    const supportResponse = await page.goto(`${config.origin}/admin/orders/${support.id}`);
    assert.equal(supportResponse.status(), 200);
    assert((await page.locator('main').innerText()).includes(supportDescription));
    assert.equal(await page.locator('main script').count(), 0);
    assert.equal(await page.locator('main img').count(), 0);
    assert.equal(await page.evaluate(() => window.orderInjected), undefined);
    assert.equal(supportResponse.headers()['cache-control'], 'no-store');
    // Submit the real server-rendered logout form with its CSRF token.
    await Promise.all([
      page.waitForURL(`${config.origin}/admin/login`),
      page.getByRole('button', { name: 'Log out', exact: true }).click(),
    ]);
    assert(!(await context.cookies()).some(item => item.name === '__Host-staff_session'));
    const revoked = await context.request.get(`${config.origin}/admin/`, {
      headers: { Cookie: `__Host-staff_session=${cookie.value}` },
    });
    assert.equal(revoked.status(), 401);
    for (const path of ['/admin/orders', new URL(detailUrl).pathname,
      '/api/v1/admin/orders', `/api/v1/admin/orders/${support.id}`]) {
      assert.equal((await context.request.get(`${config.origin}${path}`)).status(), 401);
    }
    await context.close();
  }
  // This JSON is consumed privately by the parent for log-leak assertions.
  // No trace, HAR, cookie jar, screenshot or raw token artifact is written.
  process.stdout.write(JSON.stringify({
    result: 'PASS: manager/admin Chromium login, trusted TLS, Secure cookies, CSRF/Origin, real commercial/support intake, read-only snapshots/financials/consent/history, pending SMTP-independent viewing, literal support HTML, logout and replay denial',
    secrets: sensitive,
  }));
} finally {
  await browser.close();
}
