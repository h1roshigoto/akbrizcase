import { navigate, openCart, closeCart, openRequest } from './portal-actions.mjs'
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
let stage = 'setup'
let lastPage
let lastErrors = []
try {
  for (const [size, viewport] of [['desktop', { width: 1440, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
    stage = size + '.home'
    const context = await browser.newContext({ viewport, reducedMotion: 'reduce', ignoreHTTPSErrors: false })
    const page = await context.newPage(), errors = [], external = [], orderPosts = []
    lastPage = page
    lastErrors = errors
    page.on('pageerror', () => errors.push('pageerror'))
    page.on('response', response => {
      if (response.status() >= 400) errors.push(`${response.request().resourceType()}:${response.status()}`)
    })
    page.on('request', request => {
      const url = new URL(request.url())
      if (url.origin !== origin) external.push('external')
      if (url.pathname === '/api/v1/orders' && request.method() === 'POST') orderPosts.push('order')
    })
    assert.equal((await page.goto(origin))?.status(), 200)
    await page.locator('.stage').scrollIntoViewIfNeeded()
    await expect(page.locator('mtdka-viewer')).toHaveClass(/ready/)
    const dimensions = page.getByRole('button', { name: /габариты/ })
    await expect(dimensions).toHaveAttribute('aria-pressed', 'false')
    const oldViewer = await page.locator('mtdka-viewer').elementHandle()
    const point = page.getByRole('button', { name: 'Панель индикации', exact: true })
    const popup = page.getByRole('dialog', { name: 'Панель индикации', exact: true })
    const source = page.waitForResponse(r => r.url().includes('/api/v1/knowledge/search?'))
    await point.press('Enter')
    assert.equal((await source).status(), 200)
    await expect(popup).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(popup).toHaveCount(0)
    await expect(point).toBeFocused()
    await dimensions.click()
    await expect(dimensions).toHaveAttribute('aria-pressed', 'true')
    await layout(page); await shot(page, `${size}-home-light`)
    await page.getByRole('button', { name: 'Тёмная тема', exact: true }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await shot(page, `${size}-home-dark`)
    stage = size + '.catalog'
    await navigate(page, 'Запчасти')
    assert.equal(await oldViewer.evaluate(el => el.life.signal.aborted && !el.ready && el.raf === 0), true)
    const results = page.getByRole('region', { name: 'Результаты каталога' })
    const response = await context.request.get(origin + '/api/v1/catalog/items?limit=20&offset=0')
    assert.equal(response.status(), 200)
    const catalog = await response.json()
    assert(catalog.items.some(i => i.kind === 'product') && catalog.items.some(i => i.kind === 'service'))
    for (const kind of ['product', 'service']) {
      const item = catalog.items.find(i => i.kind === kind)
      await results.getByRole('article').filter({ hasText: item.sku }).getByRole('button', { name: 'Добавить в заявку', exact: true }).click()
    }
    await openCart(page)
    const drawer = page.getByRole('dialog', { name: 'Состав заявки' })
    await expect(drawer.getByRole('listitem')).toHaveCount(2)
    await layout(page); await shot(page, `${size}-catalog-dark`)
    await closeCart(page)
    stage = size + '.drawer'
    const cart = page.getByRole('button', { name: 'Открыть корзину, позиций: 2', exact: true })
    await cart.click()
    await expect(drawer).toBeVisible()
    await page.keyboard.press('Shift+Tab')
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('dialog')), true)
    await shot(page, `${size}-drawer-dark`)
    await page.keyboard.press('Escape'); await expect(drawer).toHaveCount(0)
    await expect(cart).toBeFocused()
    assert.notEqual(await page.evaluate(() => document.documentElement.style.overflow), 'hidden')
    await cart.click()
    await openRequest(page)
    stage = size + '.form'
    const heading = page.getByRole('heading', { name: 'Заявка', exact: true })
    await expect(heading).toBeVisible()
    assert.ok(await heading.evaluate(heading => heading.getBoundingClientRect().top >= document.querySelector('.hdr').getBoundingClientRect().bottom))
    await expect(page.getByRole('checkbox', { name: /явно принимаю согласие/ })).toBeEnabled({ timeout: 15000 })
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
    const wizard = page.getByRole('tab', { name: 'Подбор по шагам', exact: true })
    await wizard.click()
    await expect(wizard).toHaveAttribute('aria-selected', 'true')
    await layout(page); await shot(page, `${size}-chat-light`)
    stage = size + '.trainer'
    await navigate(page, 'Техинформация')
    for (const model of ['МТД-КА-01', 'МТД-КА-18']) {
      await page.getByRole('radio', { name: model, exact: true }).locator('..').click()
      await expect(page.getByRole('heading', { name: `Тренажёр панели ${model}`, exact: true })).toBeVisible()
      await page.getByRole('button', { name: 'Включить макет' }).click()
      await page.getByRole('button', { name: 'Пройти с предметом' }).click()
      await expect(page.getByText('Проходы: 1 · Тревоги: 1', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Пройти без предмета' })).toBeEnabled()
      await page.getByRole('button', { name: 'Сбросить тренажёр' }).click()
    }
    await layout(page); await shot(page, `${size}-trainer-light`)
    await page.getByRole('button', { name: 'Тёмная тема', exact: true }).click()
    await shot(page, `${size}-trainer-dark`)
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await page.getByRole('link', { name: 'АК Бриз — на главную', exact: true }).click()
    await page.locator('.stage').scrollIntoViewIfNeeded()
    await expect(page.locator('mtdka-viewer')).toHaveClass(/ready/)
    const stored = await page.evaluate(() => Object.keys(localStorage))
    assert.deepEqual(stored, ['theme'])
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await context.close()
    console.log('PASS visual ' + size + ' both themes, WebGL lifecycle, cart, form, documents, chat, trainer')
  }
  stage = 'breakpoints'
  const responsive = await browser.newContext({ reducedMotion: 'reduce' })
  const responsivePage = await responsive.newPage()
  await responsivePage.goto(origin + '/zayavka.html')
  for (const width of [320, 768, 980, 981, 1099, 1100, 1180, 1181, 1359, 1360]) {
    await responsivePage.setViewportSize({ width, height: 900 })
    await layout(responsivePage)
    const result = await responsivePage.locator('.hdr').evaluate(header => {
      const visible = [...header.querySelectorAll('a, button')].filter(control => control.getClientRects().length && getComputedStyle(control).visibility === 'visible')
      return visible.every(control => {
        const rect = control.getBoundingClientRect()
        return rect.left >= 0 && rect.right <= innerWidth
      })
    })
    assert.equal(result, true, `Header controls at ${width}px`)
  }
  await responsivePage.setViewportSize({ width: 320, height: 800 })
  const menu = responsivePage.getByRole('button', { name: 'Меню', exact: true })
  await expect(responsivePage.getByRole('navigation', { name: 'Основное' })).toHaveCount(0)
  await menu.press('Enter')
  const nav = responsivePage.getByRole('navigation', { name: 'Основное' })
  await expect(nav).toBeVisible()
  const firstLink = nav.getByRole('link', { name: 'Помощник', exact: true })
  await expect(firstLink).toBeFocused()
  await responsivePage.keyboard.press('Escape')
  await expect(nav).toHaveCount(0)
  await expect(menu).toBeFocused()
  await responsive.close()
  console.log('PASS responsive header and keyboard menu at breakpoints')
  stage = 'fallback'
  const fallback = await browser.newContext({ viewport: { width: 390, height: 844 } })
  await fallback.addInitScript(() => { HTMLCanvasElement.prototype.getContext = () => null })
  const page = await fallback.newPage(); await page.goto(origin)
  const poster = page.getByRole('img', { name: 'Арочный металлодетектор МТД-КА', exact: true })
  await expect(poster).toBeVisible()
  assert.ok(await poster.evaluate(img => img.decode().then(() => img.naturalWidth > 0)))
  await expect(page.locator('mtdka-viewer canvas')).toHaveCount(0)
  await shot(page, 'mobile-webgl-fallback')
  await fallback.close()
  console.log('PASS WebGL-unavailable poster fallback')
} catch (error) {
  const line = error.stack?.match(/front-integration-visual\.mjs:(\d+)/)?.[1] ?? 'unknown'
  if (screenshots && lastPage && !lastPage.isClosed()) await shot(lastPage, 'failure')
  console.error('FRONT_VISUAL_FAILED stage=' + stage + ' line=' + line)
  console.error(error.message.split('\n')[0])
  if (lastErrors.length) console.error('Browser errors:', lastErrors.join(', '))
  process.exitCode = 1
} finally { await browser.close() }
