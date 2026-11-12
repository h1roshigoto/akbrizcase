// Focused real HTTPS staff lifecycle on the full release profile.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { chromium } from '@playwright/test';
import { staffLogin } from './staff-login.mjs';

const config = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined });
try {
  const admin = await browser.newContext({ ignoreHTTPSErrors: false });
  let nextIngress = 0;
  const paced = async context => context.route('**/*', async route => {
    if (new URL(route.request().url()).pathname.startsWith('/admin') || new URL(route.request().url()).pathname.startsWith('/api')) {
      const wait = Math.max(0, nextIngress - Date.now());
      nextIngress = Math.max(nextIngress, Date.now()) + 650;
      if (wait) await new Promise(resolve => setTimeout(resolve, wait));
    }
    await route.continue();
  });
  await paced(admin);
  const page = await admin.newPage();
  await staffLogin(page, config, 'admin');
  await page.goto(config.origin + '/admin/staff');
  const form = { login: 'release-manager', role: 'manager', password: config.passwords.manager, password_confirm: config.passwords.manager };
  assert.equal((await admin.request.post(config.origin + '/admin/staff/create-account', { form, headers: { Origin: config.origin }, maxRedirects: 0 })).status(), 403);
  const csrf = await page.locator('input[name=csrf_token]').first().inputValue();
  assert.equal((await admin.request.post(config.origin + '/admin/staff/create-account', { form: { ...form, csrf_token: csrf }, headers: { Origin: 'https://foreign.invalid' }, maxRedirects: 0 })).status(), 403);
  await page.getByLabel('Login', { exact: true }).fill(form.login);
  await page.getByLabel('New password', { exact: true }).fill(form.password);
  await page.getByLabel('Confirm new password', { exact: true }).fill(form.password);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await page.waitForURL(/\/admin\/staff\/[0-9a-f-]+$/);
  const detail = page.url();
  const manager = await browser.newContext({ ignoreHTTPSErrors: false });
  await paced(manager);
  const staff = await manager.newPage();
  await staff.goto(config.origin + '/admin/login');
  await staff.getByLabel('Login', { exact: true }).fill(form.login);
  await staff.getByLabel('Password', { exact: true }).fill(form.password);
  await staff.getByRole('button', { name: 'Log in', exact: true }).click();
  await staff.waitForURL(config.origin + '/admin/');
  const managerCsrf = await staff.locator('input[name=csrf_token]').first().inputValue();
  for (const path of ['/admin/staff', '/admin/documents', '/admin/catalog/items']) {
    assert.equal((await manager.request.get(config.origin + path, { maxRedirects: 0 })).status(), 403);
  }
  for (const path of ['/admin/staff/create-account', `/admin/documents/${config.document_id}/revoke`]) {
    assert.equal((await manager.request.post(config.origin + path, { form: { ...form, csrf_token: managerCsrf }, headers: { Origin: config.origin }, maxRedirects: 0 })).status(), 403);
  }
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: 'Disable account', exact: true }).click();
  assert.equal(page.url(), detail);
  assert.equal((await manager.request.get(config.origin + '/api/v1/admin/orders', { maxRedirects: 0 })).status(), 401);
  console.log(JSON.stringify({ result: 'PASS', checks: ['staff-create', 'csrf', 'origin', 'direct-rbac', 'disable', 'session-revocation'] }));
} catch {
  console.error('RELEASE_STAFF_FAILED');
  process.exitCode = 1;
} finally {
  await browser.close();
}
