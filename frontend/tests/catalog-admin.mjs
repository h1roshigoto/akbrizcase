// Chromium -> trusted HTTPS -> SQLAdmin/FastAPI -> PostgreSQL. No route mocks.
import assert from 'node:assert/strict';
import { createInterface } from 'node:readline';
import { randomBytes, randomUUID } from 'node:crypto';
import { chromium, expect } from '@playwright/test';
import { staffLogin } from './staff-login.mjs';
import { catalogMedia } from './catalog-media.mjs';

const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const read = async () => JSON.parse((await lines.next()).value);
const config = await read();
const rpc = async action => {
  process.stdout.write(JSON.stringify({ action }) + '\n');
  assert.deepEqual(await read(), { ok: true });
};
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined });
const secrets = [];
const attack = '</textarea><script>window.catalogInjected=true</script><img src=x onerror="window.catalogInjected=true"> & \' "';
let step = 'login';
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: false });
  const errors = [];
  context.on('page', page => {
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', async dialog => { errors.push('Unexpected dialog'); await dialog.dismiss(); });
  });
  const page = await context.newPage();
  await staffLogin(page, config, 'admin');
  secrets.push(...(await context.cookies()).map(cookie => cookie.value));
  const submit = async (tab, label, expected = 303) => {
    const [response] = await Promise.all([
      tab.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.startsWith('/admin/catalog/')),
      tab.getByRole('button', { name: label, exact: true }).click(),
    ]);
    assert.equal(response.status(), expected);
    assert.equal(response.headers()['cache-control'], 'no-store');
    if (expected === 303) await tab.waitForLoadState('domcontentloaded');
  };
  const clearXss = async tab => {
    assert.equal(await tab.evaluate(() => window.catalogInjected), undefined);
    assert.equal(await tab.locator('main img, main script').count(), 0);
  };
  step = 'model draft';
  await page.getByRole('link', { name: 'Equipment models', exact: true }).click();
  await page.getByRole('link', { name: 'Create draft', exact: true }).click();
  await page.getByLabel('Code', { exact: true }).fill('MVP016-MODEL');
  await page.getByLabel('Name', { exact: true }).fill(attack);
  await page.getByLabel('Description', { exact: true }).fill(attack);
  await submit(page, 'Create draft');
  await expect(page.locator('#catalog-status')).toHaveText('draft');
  const modelPath = new URL(page.url()).pathname;
  const modelId = modelPath.split('/').at(-1);
  const models = async () => {
    const response = await context.request.get(`${config.origin}/api/v1/equipment-models`);
    assert.equal(response.status(), 200);
    return response.json();
  };
  assert(!(await models()).items.some(model => model.id === modelId));
  await clearXss(page);
  await submit(page, 'Publish');
  await expect(page.locator('#catalog-status')).toHaveText('published');
  assert((await models()).items.some(model => model.id === modelId && model.name === attack));

  const created = {};
  const publicDetail = id => context.request.get(`${config.origin}/api/v1/catalog/items/${id}`);
  for (const [kind, sku] of [['product', 'MVP016-P'], ['service', 'MVP016-S']]) {
    step = `${kind} draft and publication`;
    await page.goto(`${config.origin}/admin/catalog/items/new`);
    await page.getByLabel('SKU', { exact: true }).fill(sku);
    await page.getByLabel('Kind', { exact: true }).selectOption(kind);
    await page.getByLabel('Name', { exact: true }).fill(`MVP016 ${kind} ${attack}`);
    await page.getByLabel('Description', { exact: true }).fill(attack);
    await page.getByLabel('Attributes', { exact: false }).fill(JSON.stringify({ nested: [attack, { quoted: attack }] }));
    await page.getByLabel('Unit', { exact: true }).fill('unit');
    await page.getByLabel('Price mode', { exact: true }).selectOption(kind === 'product' ? 'fixed' : 'on_request');
    await page.getByLabel('Price (decimal', { exact: false }).fill(kind === 'product' ? '123.45' : '');
    await page.getByLabel('Tax note', { exact: false }).fill(attack);
    await page.getByLabel('Requires shipping', { exact: false }).selectOption(kind === 'product' ? 'true' : 'false');
    await submit(page, 'Create draft');
    await expect(page.locator('#catalog-revision')).toHaveText('1');
    const path = new URL(page.url()).pathname;
    const id = path.split('/').at(-1);
    assert.equal((await publicDetail(id)).status(), 404);
    await clearXss(page);
    await submit(page, 'Publish');
    await expect(page.locator('#catalog-revision')).toHaveText('2');
    const published = await publicDetail(id);
    assert.equal(published.status(), 200);
    created[kind] = { ...(await published.json()), path };
    assert.equal(created[kind].attributes.nested[0], attack);
    assert.deepEqual(JSON.parse(await page.getByLabel('Attributes', { exact: false }).inputValue()), { nested: [attack, { quoted: attack }] });
  }
  assert.equal(created.service.price, null);
  assert.equal(created.service.requires_shipping, false);

  step = 'React catalog and selection';
  const react = await context.newPage();
  await react.goto(config.origin);
  const list = react.getByRole('region', { name: 'Результаты каталога' });
  const search = async query => {
    await react.getByRole('searchbox').fill(query);
    await react.getByRole('button', { name: 'Найти', exact: true }).click();
  };
  await search('MVP016');
  await expect(list.getByRole('article')).toHaveCount(2);
  const productCard = list.getByRole('article').filter({ hasText: 'MVP016-P' });
  const serviceCard = list.getByRole('article').filter({ hasText: 'MVP016-S' });
  await expect(productCard.locator('.price')).toContainText('123,45');
  await expect(serviceCard).toContainText('Цена по запросу');
  await expect(serviceCard.locator('.price')).not.toContainText('0,00');
  await productCard.getByRole('button', { name: 'Добавить в список' }).click();
  await productCard.getByRole('button', { name: created.product.name, exact: true }).click();
  const detail = react.getByRole('region', { name: 'Карточка позиции' });
  await expect(detail.locator('.item-description')).toHaveText(attack);
  assert.equal(await detail.locator('img[src=x], script').count(), 0);
  assert.equal(await react.evaluate(() => window.catalogInjected), undefined);

  step = 'order snapshots';
  const headers = { 'Idempotency-Key': randomUUID(), 'X-Request-Token': randomBytes(32).toString('hex') };
  secrets.push(...Object.values(headers));
  const consent = await (await context.request.get(`${config.origin}/api/v1/consent-text`)).json();
  const body = {
    kind: 'commercial', source: 'site', confirmed: true,
    items: [created.product, created.service].map(row => ({ item_id: row.id, qty: 2, expected_revision: row.revision })),
    contact: { name: 'Synthetic MVP016 buyer', email: 'mvp016@example.invalid' },
    consent: { version: consent.version, accepted: true },
  };
  const response = await context.request.post(`${config.origin}/api/v1/orders`, { headers, data: body });
  assert.equal(response.status(), 201);
  const receipt = await response.json();
  assert.equal(receipt.known_subtotal, '246.90');
  assert.equal(receipt.items_total, null);
  await rpc('snapshot');

  step = 'stale form and edit';
  await page.goto(`${config.origin}${created.product.path}`);
  const old = await context.newPage();
  await old.goto(page.url());
  await expect(old.locator('input[name=expected_revision]').first()).toHaveValue('2');
  await page.getByLabel('Name', { exact: true }).fill(`MVP016 renamed ${attack}`);
  await page.getByLabel('Price (decimal', { exact: false }).fill('234.56');
  await submit(page, 'Save changes');
  await expect(page.locator('#catalog-revision')).toHaveText('3');
  await submit(old, 'Save changes', 409);
  await expect(old.getByRole('alert')).toContainText('Reload current data');
  await rpc('unchanged');
  await clearXss(page);

  step = 'React updated price and saved revision';
  await react.reload();
  const selected = react.getByRole('complementary', { name: 'Выбранные позиции' });
  await expect(selected.getByText(/Позиция обновилась/)).toBeVisible();
  const stored = await react.evaluate(() => JSON.parse(sessionStorage.getItem('akbrizcase.selected-items')));
  assert.equal(stored.find(row => row.item_id === created.product.id).revision, 2);
  await search('MVP016 renamed');
  await expect(list.getByRole('article')).toHaveCount(1);
  await expect(list.getByRole('article').locator('.price')).toContainText('234,56');
  const updated = await (await publicDetail(created.product.id)).json();
  assert.equal(updated.revision, 3);
  assert.equal(updated.price, '234.56');

  step = 'MVP-026 catalog images';
  await catalogMedia({ browser, context, page, origin: config.origin, created, submit });
  await rpc('unchanged');

  step = 'archive service and model';
  await page.goto(`${config.origin}${created.service.path}`);
  await submit(page, 'Archive');
  await expect(page.locator('#catalog-status')).toHaveText('archived');
  const hidden = await publicDetail(created.service.id);
  assert.equal(hidden.status(), 404);
  const missing = await publicDetail(randomUUID());
  const hiddenBody = await hidden.json();
  assert.deepEqual(hiddenBody.error, (await missing.json()).error);
  const publicList = await context.request.get(`${config.origin}/api/v1/catalog/items?q=MVP016`);
  assert.equal((await publicList.json()).total, 1);
  await react.reload();
  await search('MVP016');
  await expect(list.getByRole('article')).toHaveCount(1);
  await page.goto(`${config.origin}${modelPath}`);
  await submit(page, 'Archive');
  await expect(page.locator('#catalog-status')).toHaveText('archived');
  assert(!(await models()).items.some(model => model.id === modelId));
  await rpc('unchanged');

  step = 'manager denial and security';
  const manager = await browser.newContext({ ignoreHTTPSErrors: false });
  const staff = await manager.newPage();
  await staffLogin(staff, config, 'manager');
  secrets.push(...(await manager.cookies()).map(cookie => cookie.value));
  assert.equal(await staff.getByRole('link', { name: 'Catalog items', exact: true }).count(), 0);
  const csrf = await staff.locator('input[name=csrf_token]').first().inputValue();
  secrets.push(csrf);
  for (const path of ['/admin/catalog-items', '/admin/equipment-models', created.product.path, '/admin/catalog/models/new', `${created.product.path}/image`, `${created.product.path}/applicability`]) {
    assert.equal((await manager.request.get(config.origin + path)).status(), 403);
  }
  for (const path of ['/admin/catalog/items/new', created.product.path, '/admin/catalog/models/new', `${created.product.path}/archive`, `${created.product.path}/image/replace`, `${created.product.path}/image/remove`, `${created.product.path}/applicability/publish`]) {
    assert.equal((await manager.request.post(config.origin + path, { headers: { Origin: config.origin, 'X-CSRF-Token': csrf }, form: {} })).status(), 403);
  }
  const adminCsrf = await page.locator('input[name=csrf_token]').first().inputValue();
  secrets.push(adminCsrf);
  for (const securityHeaders of [
    { Origin: config.origin },
    { Origin: config.origin, 'X-CSRF-Token': 'forged' },
    { Origin: 'https://foreign.invalid', 'X-CSRF-Token': adminCsrf },
  ]) assert.equal((await context.request.post(config.origin + created.product.path, { headers: securityHeaders, form: {} })).status(), 403);
  const replay = await context.request.post(`${config.origin}/api/v1/orders`, { headers, data: body });
  assert.equal(replay.status(), 200);
  assert.deepEqual(await replay.json(), receipt);
  await rpc('unchanged');
  assert.deepEqual(errors, []);
  process.stdout.write(JSON.stringify({ action: 'done', proof: { result: 'PASS: MVP-016 real admin forms, React, stale 409, XSS, manager denial, snapshots and replay', secrets } }) + '\n');
} catch (error) {
  throw new Error(`Catalog acceptance at ${step}: ${error.message}`);
} finally {
  await browser.close();
  process.exitCode ??= 0;
  // The protocol's stdin stays open until the parent receives the final proof.
  process.stdin.destroy();
}
