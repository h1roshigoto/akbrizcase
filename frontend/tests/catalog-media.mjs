// Synthetic JPEG/PNG -> SQLAdmin -> real AV/Pillow -> public React, without mocks.
import assert from 'node:assert/strict';
import { expect } from '@playwright/test';

export async function catalogMedia({ browser, context, page, origin, created, submit }) {
  const anonymous = await browser.newContext({ ignoreHTTPSErrors: false });
  try {
    const react = await anonymous.newPage();
    const detail = async id => (await anonymous.request.get(`${origin}/api/v1/catalog/items/${id}`)).json();
    const show = async () => {
      await react.goto(origin);
      await react.getByRole('searchbox').fill('MVP016');
      await react.getByRole('button', { name: 'Найти', exact: true }).click();
    };
    const loaded = async img => {
      await expect(img).toBeVisible();
      await expect.poll(() => img.evaluate(el => el.complete && el.naturalWidth > 0), { timeout: 15000 }).toBe(true);
    };
    const attached = {};
    for (const [kind, file] of [['product', 'frontend/tests/fixtures/mvp026-product.jpg'], ['service', 'frontend/tests/fixtures/mvp026-service.png']]) {
      const item = created[kind];
      await page.goto(`${origin}${item.path}/image`);
      await page.getByLabel('Image file', { exact: true }).setInputFiles(file);
      await submit(page, 'Upload image');
      attached[kind] = await detail(item.id);
      assert.equal(attached[kind].revision, (kind === 'product' ? 3 : 2) + 1);
      assert(attached[kind].image_url);
      await loaded(page.locator('main img'));
    }
    await show();
    const results = react.getByRole('region', { name: 'Результаты каталога' });
    for (const kind of ['product', 'service']) {
      const card = results.getByRole('article').filter({ hasText: created[kind].sku });
      await loaded(card.locator('img'));
      await expect(card.locator('img')).toHaveAttribute('src', attached[kind].image_url);
    }
    const product = results.getByRole('article').filter({ hasText: created.product.sku });
    await product.getByRole('button', { name: 'Добавить в список' }).click();
    await loaded(react.locator('.selected-image img'));
    await product.getByRole('button', { name: attached.product.name, exact: true }).click();
    await loaded(react.locator('.item-detail > .item-image img'));
    await expect(react.locator('.item-detail > .item-image img')).toHaveAttribute('src', attached.product.image_url);
    assert.deepEqual(await anonymous.cookies(), []);
    await react.screenshot({ path: '/tmp/mvp026-react-images.png', fullPage: true });

    await page.goto(`${origin}${created.product.path}/image`);
    await page.getByLabel('Image file', { exact: true }).setInputFiles('frontend/tests/fixtures/mvp026-service.png');
    await submit(page, 'Replace image');
    const replacement = await detail(created.product.id);
    assert.equal(replacement.revision, attached.product.revision + 1);
    assert.notEqual(replacement.image_url, attached.product.image_url);
    assert.equal((await anonymous.request.get(origin + attached.product.image_url)).status(), 404);
    await show();
    await expect(results.getByRole('article').filter({ hasText: created.product.sku }).locator('img')).toHaveAttribute('src', replacement.image_url);

    await page.getByLabel('Image file', { exact: true }).setInputFiles({ name: 'invalid.png', mimeType: 'image/png', buffer: Buffer.from('SYNTHETIC invalid image') });
    await submit(page, 'Replace image', 422);
    assert.equal((await detail(created.product.id)).image_url, replacement.image_url);
    assert.equal((await anonymous.request.get(origin + replacement.image_url)).status(), 200);
    for (const kind of ['product', 'service']) {
      await page.goto(`${origin}${created[kind].path}/image`);
      await submit(page, 'Remove image');
      assert.equal((await detail(created[kind].id)).image_url, null);
    }
    await show();
    for (const kind of ['product', 'service']) {
      const card = results.getByRole('article').filter({ hasText: created[kind].sku });
      await expect(card.getByRole('img', { name: 'Изображение отсутствует' })).toBeVisible();
      assert.equal(await card.locator('img').count(), 0);
    }
    await react.screenshot({ path: '/tmp/mvp026-react-placeholder.png', fullPage: true });
    // These operations must leave the original order snapshots unchanged.
    return { images: 'JPEG product, PNG service, list/detail/selection, replace/remove, invalid rollback, anonymous React' };
  } finally {
    await anonymous.close();
  }
}
