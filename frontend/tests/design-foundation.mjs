// DESIGN-001: real synthetic API; transport faults are explicit negative tests.
// No orders, staff mutations, credentials or customer fields are captured.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { mkdir } from 'node:fs/promises'
import { chromium, expect } from '@playwright/test'

const config = JSON.parse(readFileSync(0, 'utf8'))
const origin = new URL(config.origin).origin
assert.equal(new URL(origin).hostname, 'localhost')
assert(['before', 'after'].includes(config.phase))
const browser = await chromium.launch({ args: ['--enable-unsafe-swiftshader'] })
const shotDir = config.screenshot_dir
if (shotDir) await mkdir(shotDir, { recursive: true })
const contrast = (a, b) => {
  const luminance = color => {
    const rgb = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => {
      v /= 255
      return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4
    })
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722
  }
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (high + .05) / (low + .05)
}
try {
  for (const [size, width] of [['desktop', 1440], ['tablet', 768], ['mobile', 390]]) {
    for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce', ignoreHTTPSErrors: false })
      await context.addInitScript(t => localStorage.setItem('mtdka-theme', t), theme)
      const page = await context.newPage()
      const errors = []
      page.on('pageerror', () => errors.push('pageerror'))
      let releaseLoading
      let transportFault = false
      const waiting = new Promise(resolve => { releaseLoading = resolve })
      await page.route('**/api/v1/catalog/items?*', async route => {
        await waiting
        if (transportFault) await route.abort('failed')
        else await route.continue()
      })
      await page.goto(origin + '/#catalog', { waitUntil: 'domcontentloaded' })
      const results = page.getByRole('region', { name: 'Результаты каталога' })
      await expect(results.getByRole('status')).toContainText('Загружаем каталог')
      assert.equal(await results.getAttribute('aria-busy'), 'true')
      releaseLoading()
      await expect(results.getByRole('article').first()).toBeVisible()
      const search = page.getByRole('searchbox', { name: 'Поиск по названию или SKU' })
      await search.focus()
      await page.keyboard.press('Tab')
      const submit = page.getByRole('button', { name: 'Найти', exact: true })
      await expect(submit).toBeFocused()
      if (config.phase === 'after') {
        assert.match(await submit.getAttribute('class'), /ui-button/)
        const colors = await submit.evaluate(el => {
          const s = getComputedStyle(el)
          return { text: s.color, bg: s.backgroundColor, outline: s.outlineStyle, outlineWidth: s.outlineWidth }
        })
        assert(contrast(colors.text, colors.bg) >= 4.5)
        assert.equal(colors.outline, 'solid')
        assert.equal(colors.outlineWidth, '3px')
        assert((await submit.boundingBox()).height >= 44)
        const field = await search.locator('..').evaluate(el => {
          const s = getComputedStyle(el)
          return { border: s.borderTopColor, bg: s.backgroundColor, focus: getComputedStyle(document.querySelector('.ui-button')).outlineColor }
        })
        assert(contrast(field.border, field.bg) >= 3)
        assert(contrast(field.focus, field.bg) >= 3)
      }
      const capture = shotDir && ((size === 'desktop' && theme === 'light') || (size === 'mobile' && theme === 'dark'))
      if (capture) await page.screenshot({ path: `${shotDir}/${config.phase}-${size}-${theme}-catalog.png`, fullPage: true })
      await search.fill('PRODUCT001-NO-SUCH-SYNTHETIC-SKU')
      await search.press('Enter')
      await expect(results.getByRole('heading', { name: 'Позиции не найдены' })).toBeVisible()
      await results.getByRole('status').getByRole('button', { name: 'Сбросить фильтры', exact: true }).click()
      await expect(results.getByRole('article').first()).toBeVisible()
      transportFault = true
      await search.fill('PRODUCT001-TRANSPORT-FAULT')
      await search.press('Enter')
      await expect(results.getByRole('alert')).toContainText('Не удалось загрузить данные')
      assert.equal(await results.getByRole('article').count(), 0)
      const retry = results.getByRole('button', { name: 'Повторить', exact: true })
      await retry.focus()
      if (capture) await page.screenshot({ path: `${shotDir}/${config.phase}-${size}-${theme}-error.png`, fullPage: true })
      transportFault = false
      await retry.press('Enter')
      await expect(results.getByRole('heading', { name: 'Позиции не найдены' })).toBeVisible()
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
      await results.getByRole('status').getByRole('button', { name: 'Сбросить фильтры', exact: true }).click()
      await expect(results.getByRole('article').first()).toBeVisible()
      if (config.phase === 'after') {
        await page.emulateMedia({ forcedColors: 'active' })
        await search.focus()
        await page.keyboard.press('Tab')
        assert.equal(await submit.evaluate(el => getComputedStyle(el).outlineStyle), 'solid')
      }
      assert.deepEqual(errors, [])
      await context.close()
      console.log(`PASS DESIGN001 ${config.phase} ${size} ${theme}: loading/search/empty/reset/error/retry/keyboard/layout`)
    }
  }
} finally { await browser.close() }
