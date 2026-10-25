// Real Chromium -> Nginx -> FastAPI -> disposable PostgreSQL. No route mocks.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { chromium, expect } from '@playwright/test';

const config = JSON.parse(readFileSync(0, 'utf8'));
const storageKey = 'akbrizcase.selected-items';
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
});
try {
  const context = await browser.newContext();
  if (config.selection.length) {
    await context.addInitScript(({ key, selection }) => {
      sessionStorage.setItem(key, JSON.stringify(selection));
    }, { key: storageKey, selection: config.selection });
  }
  const page = await context.newPage();
  const network = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith('/api/v1/')) return;
    assert.equal(url.origin, config.origin);
    assert.equal(response.request().method(), 'GET');
    if ((url.searchParams.get('q') || '').length > 1000)
      url.searchParams.set('q', '[1001-character-invalid-query]');
    network.push({ path: url.pathname + url.search, status: response.status() });
  });
  const list = page.getByRole('region', { name: 'Результаты каталога' });
  const selected = page.getByRole('complementary', { name: 'Выбранные позиции' });
  const cards = list.getByRole('article');
  const card = sku => list.getByRole('article').filter({ hasText: sku });
  const reset = () => list.getByRole('button', { name: 'Сбросить фильтры' }).first().click();
  const submit = async query => {
    await page.getByRole('searchbox').fill(query);
    await page.getByRole('button', { name: 'Найти', exact: true }).click();
  };
  const stored = () => page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), storageKey);
  let selection = config.selection;

  if (config.mode === 'outage') {
    await page.goto(config.origin);
    await expect(list.getByRole('alert')).toBeVisible();
    await expect(cards).toHaveCount(0);
    await expect(page.getByText('Не удалось загрузить модели.', { exact: false })).toBeVisible();
    await expect(selected.getByRole('alert')).toContainText('Часть позиций недоступна');
    await expect(selected.locator('.subtotal')).toContainText('Пока не определена');
    const failed = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/catalog/items');
    await list.getByRole('button', { name: 'Повторить', exact: true }).click();
    assert.equal((await failed).status(), 503);
    await expect(list.getByRole('alert')).toBeVisible();
    assert(network.some(row => row.path.startsWith('/api/v1/equipment-models') && row.status === 503));
    assert(network.some(row => row.path.startsWith('/api/v1/catalog/items/') && row.status === 503));
  } else {
    const firstPage = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/catalog/items');
    await page.goto(config.origin);
    const response = await firstPage;
    assert.equal(response.status(), 200);
    const catalog = await response.json();
    const bySku = Object.fromEntries(catalog.items.map(item => [item.sku, item]));
    if (config.mode === 'live') {
      assert.equal(catalog.total, 4);
      assert.deepEqual(Object.keys(bySku).sort(), ['DEMO-P-001', 'DEMO-P-002', 'DEMO-P-003', 'DEMO-S-001']);
      assert.equal(bySku['DEMO-S-001'].kind, 'service');
      assert.equal(bySku['DEMO-P-001'].price, '123.45');
      assert.equal(bySku['DEMO-P-002'].price, null);
      assert(catalog.items.every(item => item.image_url === null));
      await expect(cards).toHaveCount(4);
      await expect(list.getByRole('img', { name: 'Изображение отсутствует' })).toHaveCount(4);
      await expect(card('DEMO-P-002')).toContainText('Цена по запросу');
      await expect(card('DEMO-P-002').locator('.price')).not.toContainText('0,00');
      await expect(card('DEMO-P-003').locator('.price')).toContainText('0,00');
      await card('DEMO-P-001').getByRole('button', { name: 'Добавить в список' }).click();
      await page.getByRole('button', { name: 'Услуги', exact: true }).click();
      await expect(cards).toHaveCount(1);
      await card('DEMO-S-001').getByRole('button', { name: 'Добавить в список' }).click();
      await expect(selected.getByRole('listitem')).toHaveCount(2);
      await expect(selected.locator('.subtotal')).toContainText('191,34');
      await selected.getByRole('button', { name: 'Увеличить количество DEMO-P-001', exact: true }).click();
      await expect(selected.locator('.subtotal')).toContainText('314,79');
      const qty = selected.getByRole('textbox', { name: 'Количество DEMO-P-001', exact: true });
      await qty.fill('0');
      await expect(qty).toHaveAttribute('aria-invalid', 'true');
      await qty.blur();
      await expect(qty).toHaveValue('2');
      await qty.fill('999');
      await expect(selected.getByRole('button', { name: 'Увеличить количество DEMO-P-001', exact: true })).toBeDisabled();
      await qty.fill('2');
      await reset();
      await expect(cards).toHaveCount(4);
      await card('DEMO-P-002').getByRole('button', { name: 'Добавить в список' }).click();
      await expect(selected.getByRole('listitem')).toHaveCount(3);
      await expect(selected.locator('.subtotal')).toContainText('Известная часть суммы');
      await expect(selected.locator('.subtotal')).toContainText('314,79');
      await selected.getByRole('button', { name: 'Удалить DEMO-P-002', exact: true }).click();
      await expect(selected.getByRole('listitem')).toHaveCount(2);
      const searchResponse = page.waitForResponse(r => new URL(r.url()).searchParams.get('q') === 'DEMO-P-001');
      await submit('DEMO-P-001');
      const search = await (await searchResponse).json();
      assert.equal(search.total, 1);
      assert.equal(search.items[0].id, bySku['DEMO-P-001'].id);
      await expect(cards).toHaveCount(1);
      await card('DEMO-P-001').getByRole('button', { name: bySku['DEMO-P-001'].name, exact: true }).click();
      const detail = page.getByRole('region', { name: 'Карточка позиции' });
      await expect(detail.locator('.item-description')).toContainText('Синтетический пример');
      // Keep the open detail while changing model context: the list must be
      // empty and detail must truthfully say unknown, never confirmed.
      const model = page.getByRole('combobox', { name: 'Модель оборудования' });
      await expect(model).toBeEnabled();
      const modelId = await model.locator('option').nth(1).getAttribute('value');
      assert(modelId);
      const modelDetail = page.waitForResponse(r => {
        const url = new URL(r.url());
        return url.pathname === `/api/v1/catalog/items/${bySku['DEMO-P-001'].id}` && url.searchParams.get('model_id') === modelId;
      });
      await model.selectOption(modelId);
      assert.equal((await (await modelDetail).json()).applicability.status, 'unknown');
      await expect(detail.getByText('Применимость не подтверждена', { exact: true })).toBeVisible();
      await detail.getByRole('button', { name: 'Вернуться в каталог', exact: false }).click();
      await expect(list.getByRole('heading', { name: 'Позиции не найдены' })).toBeVisible();
      await expect(cards).toHaveCount(0);
      assert(network.some(row => row.path.includes(`model_id=${modelId}`) && row.status === 200));
      await reset();
      await expect(cards).toHaveCount(4);
      await submit('NO-SUCH-SYNTHETIC-SKU');
      await expect(list.getByRole('heading', { name: 'Позиции не найдены' })).toBeVisible();
      // Bypass only the input's length guard to exercise a genuine FastAPI 422.
      // The transport and response are untouched.
      await page.getByRole('searchbox').evaluate(input => input.removeAttribute('maxlength'));
      const invalid = page.waitForResponse(r => new URL(r.url()).searchParams.get('q')?.length === 1001);
      await submit('x'.repeat(1001));
      assert.equal((await invalid).status(), 422);
      await expect(list.getByRole('alert')).toBeVisible();
      await expect(cards).toHaveCount(0);
      await reset();
      await expect(cards).toHaveCount(4);
      selection = await stored();
      assert.equal(selection.length, 2);
      for (const row of selection) assert.deepEqual(Object.keys(row).sort(), ['item_id', 'qty', 'revision']);
      await page.reload();
      await expect(selected.getByRole('listitem')).toHaveCount(2);
      await expect(selected.locator('.subtotal')).toContainText('314,79');
    } else {
      assert.equal(catalog.total, 3);
      assert(!bySku['DEMO-S-001']);
      await expect(cards).toHaveCount(3);
      await expect(selected.getByText(/Позиция обновилась/)).toBeVisible();
      await expect(selected.getByRole('alert')).toContainText('Часть позиций недоступна');
      await expect(selected.locator('.subtotal')).toContainText('300,00');
      assert.deepEqual(await stored(), config.selection, 'saved revisions must not silently change');
      await card('DEMO-P-001').getByRole('button', { name: bySku['DEMO-P-001'].name, exact: true }).click();
      const detail = page.getByRole('region', { name: 'Карточка позиции' });
      await expect(detail.locator('.item-description')).toContainText('<img src=x onerror=');
      assert.equal(await detail.locator('.item-description img').count(), 0);
      assert.equal(await page.evaluate(() => window.catalogInjected), undefined);
      // The archived service stays in the restored selection with the existing
      // unavailable notice and explicit removal/retry controls.
      assert(network.some(row => row.path.startsWith('/api/v1/catalog/items/') && row.status === 404));
      await selected.getByRole('button', { name: 'Повторить загрузку', exact: true }).click();
      await expect(selected.getByRole('alert')).toContainText('Часть позиций недоступна');
    }
    assert(network.some(row => row.path.startsWith('/api/v1/equipment-models') && row.status === 200));
  }
  await expect(page.locator('.demo-banner')).toHaveCount(0);
  assert.equal(await page.evaluate(() => localStorage.length), 0);
  assert.deepEqual(await page.evaluate(() => Object.keys(sessionStorage)), [storageKey]);
  assert.deepEqual(errors, []);
  process.stdout.write(JSON.stringify({ result: 'PASS', scenario: config.mode, selection, network }));
} finally {
  await browser.close();
}
