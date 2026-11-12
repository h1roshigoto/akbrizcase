// Focused real HTTPS staff lifecycle on the full release profile.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { chromium } from '@playwright/test';
import { staffLogin } from './staff-login.mjs';

const config = JSON.parse(readFileSync(0, 'utf8'));
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined });
let stage = 'ADMIN_LOGIN';
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
  stage = 'STAFF_PAGE';
  assert.equal((await page.goto(config.origin + '/admin/staff')).status(), 200);
  stage = 'CSRF_ORIGIN';
  const form = { login: 'release-manager', role: 'manager', password: config.passwords.manager, password_confirm: config.passwords.manager };
  assert.equal((await admin.request.post(config.origin + '/admin/staff/create-account', { form, headers: { Origin: config.origin }, maxRedirects: 0 })).status(), 403);
  const csrf = await page.locator('input[name=csrf_token]').first().inputValue();
  assert.equal((await admin.request.post(config.origin + '/admin/staff/create-account', { form: { ...form, csrf_token: csrf }, headers: { Origin: 'https://foreign.invalid' }, maxRedirects: 0 })).status(), 403);
  stage = 'CREATE_FORM';
  await page.getByLabel('Login', { exact: true }).fill(form.login);
  await page.getByLabel('New password', { exact: true }).fill(form.password);
  await page.getByLabel('Confirm new password', { exact: true }).fill(form.password);
  assert.equal(await page.locator('form[action="/admin/staff/create-account"]').evaluate(form => form.checkValidity()), true);
  const created = page.waitForResponse(response => new URL(response.url()).pathname === '/admin/staff/create-account');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  assert.equal((await created).status(), 303);
  await page.waitForURL(/\/admin\/staff\/[0-9a-f-]+$/);
  const detail = page.url();
  stage = 'MANAGER_LOGIN';
  const manager = await browser.newContext({ ignoreHTTPSErrors: false });
  await paced(manager);
  const staff = await manager.newPage();
  await staff.goto(config.origin + '/admin/login');
  await staff.getByLabel('Login', { exact: true }).fill(form.login);
  await staff.getByLabel('Password', { exact: true }).fill(form.password);
  const login = staff.waitForResponse(response => new URL(response.url()).pathname === '/admin/auth/login');
  await staff.getByRole('button', { name: 'Log in', exact: true }).click();
  assert.equal((await login).status(), 303);
  await staff.waitForURL(config.origin + '/admin/');
  stage = 'DIRECT_RBAC';
  const managerCsrf = await staff.locator('input[name=csrf_token]').first().inputValue();
  for (const path of ['/admin/staff', '/admin/documents', '/admin/catalog-items']) {
    assert.equal((await manager.request.get(config.origin + path, { maxRedirects: 0 })).status(), 403);
  }
  for (const path of ['/admin/staff/create-account', `/admin/documents/${config.document_id}/revoke`]) {
    assert.equal((await manager.request.post(config.origin + path, { form: { ...form, csrf_token: managerCsrf }, headers: { Origin: config.origin }, maxRedirects: 0 })).status(), 403);
  }
  stage = 'DISABLE';
  await page.getByRole('checkbox').check();
  const disabled = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/disable'));
  await page.getByRole('button', { name: 'Disable account', exact: true }).click();
  assert.equal((await disabled).status(), 303);
  assert.equal(page.url(), detail);
  stage = 'SESSION_REVOCATION';
  assert.equal((await manager.request.get(config.origin + '/api/v1/admin/orders', { maxRedirects: 0 })).status(), 401);
  console.log(JSON.stringify({ result: 'PASS', checks: ['staff-create', 'csrf', 'origin', 'direct-rbac', 'disable', 'session-revocation'] }));
} catch (error) {
  const line = [...(error?.stack?.matchAll(/release-staff\.mjs:(\d+):\d+/g) ?? [])][0]?.[1] ?? 'unknown';
  const actual = typeof error?.actual === 'number' ? error.actual : 'NONE';
  console.error(`RELEASE_STAFF_FAILED_${stage}_LINE_${line}_ACTUAL_${actual}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
