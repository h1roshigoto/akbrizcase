// Presentation regression only: intercepted synthetic responses, no database or
// commercial submission. This does not certify the original archive or live API.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { mkdir } from 'node:fs/promises'
import { chromium, expect } from '@playwright/test'

const config = JSON.parse(readFileSync(0, 'utf8'))
const origin = new URL(config.origin).origin
assert(['localhost', '127.0.0.1'].includes(new URL(origin).hostname))
const fixtures = JSON.parse(readFileSync(new URL('../src/api/fixtures.json', import.meta.url)))
const items = structuredClone(fixtures.catalog_page.items)
items[0].sku = 'ЯИУШ.301421.011-02'
items[0].image_url = `/api/v1/catalog/items/${items[0].id}/image/${'a'.repeat(64)}.png`
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined })
if (config.screenshot_dir) await mkdir(config.screenshot_dir, { recursive: true })
let checks = 0
try {
  for (const [size, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport, reducedMotion: 'reduce' })
      try {
        const page = await context.newPage()
        const imageRequests = [], errors = []
        page.on('pageerror', error => errors.push(error.message))
        await page.addInitScript(value => localStorage.setItem('mtdka-theme', value), theme)
        await page.route('**/api/v1/**', route => {
          const request = route.request(), url = new URL(request.url())
          assert.equal(request.method(), 'GET', 'Presentation test must never submit data')
          if (url.pathname.includes('/image/')) {
            imageRequests.push(url.pathname)
            return route.abort()
          }
          if (url.pathname === '/api/v1/equipment-models') return route.fulfill({ json: fixtures.models })
          if (url.pathname === '/api/v1/catalog/items') return route.fulfill({ json: { items, total: items.length, limit: 20, offset: 0 } })
          const item = items.find(row => url.pathname === `/api/v1/catalog/items/${row.id}`)
          assert(item, `Unexpected API call: ${url.pathname}`)
          return route.fulfill({ json: { ...item, description: 'SYNTHETIC ONLY — presentation check', attributes: {} } })
        })
        const shot = async name => {
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
          await page.evaluate(async () => {
            document.activeElement?.blur()
            window.scrollTo(0, 0)
            await new Promise(requestAnimationFrame)
            await new Promise(requestAnimationFrame)
          })
          if (config.screenshot_dir) await page.screenshot({ path: `${config.screenshot_dir}/${size}-${theme}-${name}.png`, fullPage: true })
        }
        await page.goto(origin + '/#catalog')
        await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
        const card = item => page.getByRole('region', { name: 'Результаты каталога' }).getByRole('article').filter({ hasText: item.sku })
        await expect(card(items[0]).getByRole('img', { name: 'На заказ', exact: true })).toHaveText('На заказ')
        await expect(card(items[0]).locator('img')).toHaveCount(0)
        await expect(card(items[0]).locator('.price')).toContainText('1 200,00')
        await expect(card(items[1]).getByRole('img', { name: 'Сервисная услуга' })).toBeVisible()
        await expect(card(items[1])).not.toContainText('Изображение отсутствует')
        await expect(card(items[2]).getByRole('img', { name: 'Изображение отсутствует' })).toBeVisible()
        await shot('catalog-synthetic')
        for (const item of items.slice(0, 2)) {
          await card(item).getByRole('button', { name: item.name, exact: true }).click()
          const detail = page.getByRole('region', { name: 'Карточка позиции' })
          await expect(detail.getByRole('img', { name: item.kind === 'service' ? 'Сервисная услуга' : 'На заказ', exact: true })).toBeVisible()
          await expect(detail.locator('img')).toHaveCount(0)
          await expect(detail).not.toContainText('Изображение отсутствует')
          if (item.kind === 'product') await shot('left-post-detail-synthetic')
          await detail.getByRole('button', { name: /Вернуться в каталог/ }).click()
        }
        assert.deepEqual(imageRequests, [], 'The overridden product photo must not be requested')
        assert.deepEqual(errors, [])
        checks++
      } finally { await context.close() }
    }
  }
  console.log(JSON.stringify({ presentation: 'PASS', configurations: checks, data: 'INTERCEPTED_SYNTHETIC_ONLY', real_backend: 'NOT_TESTED', original_archive: 'UNAVAILABLE' }))
} finally { await browser.close() }
