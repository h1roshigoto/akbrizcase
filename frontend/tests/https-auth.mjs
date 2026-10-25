// Real Chromium -> TLS Nginx -> FastAPI -> PostgreSQL. No authentication mocks.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { chromium } from '@playwright/test';

const config = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
});
const sensitive = [];
try {
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
    await context.close();
  }
  // This JSON is consumed privately by the parent for log-leak assertions.
  // No trace, HAR, cookie jar, screenshot or raw token artifact is written.
  process.stdout.write(JSON.stringify({
    result: 'PASS: manager/admin Chromium login, trusted TLS, Secure cookie delivery, CSRF/Origin, logout and replay',
    secrets: sensitive,
  }));
} finally {
  await browser.close();
}
