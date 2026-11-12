// Public, synthetic-only visual smoke against a REAL running API. No API mocks,
// POST /orders, admin mutation, withdrawal or document revocation. Linked E2E
// owns those business assertions on its separate disposable database.
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { chromium, expect } from '@playwright/test'

const config = JSON.parse(readFileSync(0, 'utf8'))
const origin = new URL(config.origin).origin
assert(['localhost', '127.0.0.1'].includes(new URL(origin).hostname))
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined, args: ['--enable-unsafe-swiftshader'] })
const screenshots = config.screenshot_dir
if (screenshots) await mkdir(screenshots, { recursive: true })
const shot = async (page, name) => { if (screenshots) await page.screenshot({ path: `${screenshots}/${name}.png`, fullPage: true }) }
const layout = async page => {
  assert.equal(await page.getByRole('main').count(), 1)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
}
const navigate = async (page, name) => {
  const link = page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name, exact: true })
  if (!await link.isVisible()) await page.getByRole('button', { name: 'Меню', exact: true }).click()
  await link.click()
}
let stage = 'setup'
let lastPage
try {
  for (const [size, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    stage = size + '.home'
    const context = await browser.newContext({ viewport, reducedMotion: 'reduce', ignoreHTTPSErrors: false })
    const page = await context.newPage(), errors = [], external = [], orderPosts = []
    lastPage = page
    page.on('pageerror', () => errors.push('pageerror'))
    page.on('request', request => {
      const url = new URL(request.url())
      if (url.origin !== origin) external.push('external')
      if (url.pathname === '/api/v1/orders' && request.method() === 'POST') orderPosts.push('order')
    })
    await page.goto(origin)
    await expect(page.locator('mtdka-viewer')).toHaveClass(/ready/)
    await expect(page.getByRole('button', { name: 'Показать габариты', exact: true })).toBeEnabled()
    const oldViewer = await page.locator('mtdka-viewer').elementHandle()
    await page.getByRole('button', { name: 'Верхняя панель макета', exact: true }).click()
    await expect(page.getByRole('region', { name: 'Верхняя панель макета' })).toBeVisible()
    await page.getByRole('button', { name: 'Закрыть подсказку' }).click()
    await page.getByRole('button', { name: 'Показать габариты', exact: true }).click()
    await layout(page); await shot(page, `${size}-home-light`)
    await page.getByRole('button', { name: 'Тёмная тема', exact: true }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await shot(page, `${size}-home-dark`)
    stage = size + '.catalog'
    await navigate(page, 'Каталог')
    assert.equal(await oldViewer.evaluate(el => el.life.signal.aborted && !el.ready && el.raf === 0), true)
    const results = page.getByRole('region', { name: 'Результаты каталога' })
    const response = await context.request.get(origin + '/api/v1/catalog/items?limit=20&offset=0')
    assert.equal(response.status(), 200)
    const catalog = await response.json()
    assert(catalog.items.some(i => i.kind === 'product') && catalog.items.some(i => i.kind === 'service'))
    for (const kind of ['product', 'service']) {
      const item = catalog.items.find(i => i.kind === kind)
      await results.getByRole('article').filter({ hasText: item.sku }).getByRole('button', { name: 'Добавить в список', exact: true }).click()
    }
    await expect(page.getByRole('complementary', { name: 'Выбранные позиции' }).getByRole('listitem')).toHaveCount(2)
    await layout(page); await shot(page, `${size}-catalog-dark`)
    stage = size + '.drawer'
    const cart = page.getByRole('button', { name: 'Открыть корзину, позиций: 2', exact: true })
    await cart.click()
    const drawer = page.getByRole('dialog', { name: 'Состав заявки' })
    await expect(drawer).toBeVisible()
    await page.keyboard.press('Shift+Tab')
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('dialog')), true)
    await shot(page, `${size}-drawer-dark`)
    await page.keyboard.press('Escape'); await expect(drawer).toHaveCount(0)
    await expect(cart).toBeFocused()
    await cart.click()
    await drawer.getByRole('button', { name: 'Оформить заявку', exact: true }).click()
    stage = size + '.form'
    await expect(page.getByRole('heading', { name: 'Оформление заявки', exact: true })).toBeVisible()
    await expect(page.getByRole('checkbox', { name: /явно принимаю согласие/ })).toBeEnabled()
    await page.getByRole('button', { name: 'Подтвердить и отправить заявку' }).click()
    await expect(page.getByText('Укажите имя: от 1 до 200 символов.', { exact: true })).toBeVisible()
    assert.deepEqual(orderPosts, [])
    await layout(page); await shot(page, `${size}-request-dark`)
    await page.getByRole('button', { name: 'Тёмная тема', exact: true }).click()
    await shot(page, `${size}-request-light`)
    stage = size + '.documents'
    await navigate(page, 'Нормативная база')
    const materials = page.getByRole('region', { name: 'Опубликованные материалы' })
    await expect(materials.getByRole('article').first()).toBeVisible()
    for (const link of await materials.getByRole('link', { name: 'Открыть документ', exact: true }).all())
      assert.match(await link.getAttribute('href'), /^\/api\/v1\/documents\/[0-9a-f-]+\/versions\/[0-9a-f-]+\/file$/)
    await layout(page); await shot(page, `${size}-documents-light`)
    stage = size + '.chat'
    await navigate(page, 'Помощник')
    await expect(page.getByRole('article', { name: 'Актуальный ответ', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Подбор по шагам', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Подбор по шагам', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await layout(page); await shot(page, `${size}-chat-light`)
    stage = size + '.trainer'
    await navigate(page, 'Техинформация')
    for (const model of ['МТД-КА-01', 'МТД-КА-18']) {
      await page.getByRole('radio', { name: model, exact: true }).check()
      await page.getByRole('button', { name: 'Включить макет' }).click()
      await page.getByRole('button', { name: 'Пройти с предметом' }).click()
      await expect(page.getByText('Проходы: 1 · Тревоги: 1', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Пройти без предмета' })).toBeEnabled()
      await page.getByRole('button', { name: 'Сбросить тренажёр' }).click()
    }
    await layout(page); await shot(page, `${size}-trainer-light`)
    await page.getByRole('button', { name: 'Тёмная тема', exact: true }).click()
    await shot(page, `${size}-trainer-dark`)
    await navigate(page, 'Главная')
    await expect(page.locator('mtdka-viewer')).toHaveClass(/ready/)
    const stored = await page.evaluate(() => Object.keys(localStorage))
    assert.deepEqual(stored, ['mtdka-theme'])
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await context.close()
    console.log('PASS visual ' + size + ' both themes, WebGL lifecycle, cart, form, documents, chat, trainer')
  }
  stage = 'fallback'
  const fallback = await browser.newContext({ viewport: { width: 390, height: 844 } })
  await fallback.addInitScript(() => { HTMLCanvasElement.prototype.getContext = () => null })
  const page = await fallback.newPage(); await page.goto(origin)
  await expect(page.getByText('3D недоступно. Показано изображение макета.')).toBeVisible()
  await expect(page.getByRole('img', { name: /визуальный макет/ })).toBeVisible()
  await shot(page, 'mobile-webgl-fallback')
  await fallback.close()
  console.log('PASS WebGL-unavailable poster fallback')
} catch (error) {
  const line = error.stack?.match(/front-integration-visual\.mjs:(\d+)/)?.[1] ?? 'unknown'
  if (screenshots && lastPage && !lastPage.isClosed()) await shot(lastPage, 'failure')
  console.error('FRONT_VISUAL_FAILED stage=' + stage + ' line=' + line)
  process.exitCode = 1
} finally { await browser.close() }
