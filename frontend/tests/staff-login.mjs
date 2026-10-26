// Shared real HTTPS form login. Authority always comes from PostgreSQL.
import assert from 'node:assert/strict';

export async function staffLogin(page, config, role) {
  const initial = await page.goto(`${config.origin}/admin/login`);
  assert.equal(initial.status(), 200);
  assert.equal(await page.evaluate(() => isSecureContext), true);
  await page.getByLabel('Login', { exact: true }).fill(`browser-${role}`);
  await page.getByLabel('Password', { exact: true }).fill(config.passwords[role]);
  const [response] = await Promise.all([
    page.waitForResponse(r => new URL(r.url()).pathname === '/admin/auth/login'),
    page.getByRole('button', { name: 'Log in', exact: true }).click(),
  ]);
  assert.equal(response.status(), 303);
  await page.waitForURL(`${config.origin}/admin/`);
  await page.getByRole('button', { name: 'Log out', exact: true }).waitFor();
}
