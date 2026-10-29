import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
let input = ''
for await (const chunk of process.stdin) input += chunk
const config = JSON.parse(input)
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE || undefined })
const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await context.newPage()
let creations = 0, orders = 0, errors = 0
page.on('pageerror', () => errors++)
page.on('request', request => {
  if (request.url().endsWith('/api/v1/chat/sessions') && request.method() === 'POST') creations++
  if (request.url().endsWith('/api/v1/orders') && request.method() === 'POST') orders++
})
const current = () => page.getByRole('article', { name: 'Актуальный ответ', exact: true })
try {
  await page.goto(config.origin)
  await page.getByRole('heading', { name: 'Товары и услуги', exact: true }).waitFor()
  assert.equal(creations, 0)
  await page.getByRole('link', { name: 'Помощник', exact: true }).click()
  await current().waitFor()
  assert.equal(creations, 1)
  await page.getByRole('button', { name: 'Документация', exact: true }).click()
  await page.getByLabel('Запрос по документации').fill('Синтетическийквантопылесос')
  await page.getByRole('button', { name: 'Отправить запрос', exact: true }).click()
  await current().getByText(/точная цитата для проверки/).waitFor()
  const expected = `/api/v1/documents/${config.document_id}/versions/${config.version_id}/file`
  assert.equal(await current().getByRole('link').first().getAttribute('href'), expected)
  await page.getByRole('button', { name: 'Товары и услуги', exact: true }).click()
  await page.getByLabel('Поиск товаров и услуг', { exact: true }).fill('DEMO-P-001')
  await page.getByRole('button', { name: 'Отправить запрос', exact: true }).click()
  await current().getByText('DEMO-P-001', { exact: true }).waitFor()
  assert.equal(await current().getByText('Применимость подтверждена', { exact: true }).count(), 0)
  // A commercial action must open a reviewable form, with one visible page
  // across real same-document Back/Forward; entered form values stay in memory.
  await current().getByRole('button', { name: 'Проверить коммерческую заявку', exact: true }).click()
  await page.getByRole('heading', { name: 'Оформление заявки', exact: true }).waitFor()
  await page.getByLabel('Комментарий').fill('SYNTHETIC NAVIGATION')
  const selectionBeforeNavigation = await page.evaluate(() => sessionStorage.getItem('akbrizcase.selected-items'))
  await page.goBack()
  await current().waitFor()
  await page.goBack()
  await page.getByRole('heading', { name: 'Товары и услуги', exact: true }).waitFor()
  await expect(page.getByRole('main')).toHaveCount(1)
  await page.goForward()
  await current().waitFor()
  await page.goForward()
  await page.getByRole('heading', { name: 'Оформление заявки', exact: true }).waitFor()
  await expect(page.getByLabel('Комментарий')).toHaveValue('SYNTHETIC NAVIGATION')
  await expect(page.getByRole('main')).toHaveCount(1)
  assert.equal(await page.evaluate(() => sessionStorage.getItem('akbrizcase.selected-items')), selectionBeforeNavigation)
  await page.getByRole('button', { name: /Вернуться в каталог/ }).click()
  await page.getByRole('heading', { name: 'Товары и услуги', exact: true }).waitFor()
  assert.equal(new URL(page.url()).hash, '#catalog')
  await page.getByRole('link', { name: 'Помощник', exact: true }).click()
  await current().waitFor()
  await current().getByRole('button', { name: /^Открыть карточку:/ }).click()
  await page.getByText('DEMO-P-001', { exact: true }).first().waitFor()
  await page.getByRole('link', { name: 'Помощник', exact: true }).click()
  await current().waitFor()
  await page.getByRole('button', { name: 'Диагностика', exact: true }).click()
  await page.getByLabel('Модель для диагностики').selectOption(config.model_id)
  await page.getByRole('button', { name: 'Синтетическая проверка оборудования', exact: true }).click()
  await current().getByText('Синтетический вопрос диагностики', { exact: true }).waitFor()
  await page.getByRole('button', { name: 'Синтетическое наблюдение', exact: true }).click()
  await current().getByText('Синтетический утверждённый результат', { exact: true }).waitFor()
  await page.getByRole('button', { name: 'Помогло', exact: true }).click()
  await current().getByText('Диагностика завершена', { exact: true }).waitFor()
  await page.reload()
  await current().getByText('Диагностика завершена', { exact: true }).waitFor()
  assert.equal(creations, 1)
  assert.equal(orders, 0)
  assert.equal(errors, 0)
  assert.equal(await page.evaluate(() => localStorage.length), 0)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  // Check storage privately in-page; never export tokens, bodies or excerpts.
  assert.equal(await page.evaluate(() => {
    const owner = JSON.parse(sessionStorage.getItem('akbrizcase.chat.v1'))
    return Object.keys(owner).sort().join(',') === 'expires_at,pending,session_id,session_token' && owner.pending === null
  }), true)
  // Test the boundary against 50 real, published catalog positions. The
  // second tab stores only the ordinary minimal selection, never chat content.
  const limitPage = await context.newPage()
  limitPage.on('pageerror', () => errors++)
  limitPage.on('request', request => {
    if (request.url().endsWith('/api/v1/orders') && request.method() === 'POST') orders++
  })
  await limitPage.addInitScript(rows => sessionStorage.setItem('akbrizcase.selected-items', JSON.stringify(rows)), config.limit_selection)
  await limitPage.goto(config.origin + '/#chat')
  const limitReply = limitPage.getByRole('article', { name: 'Актуальный ответ', exact: true })
  await limitReply.waitFor()
  await limitPage.getByRole('button', { name: 'Товары и услуги', exact: true }).click()
  await limitPage.getByLabel('Поиск товаров и услуг', { exact: true }).fill('DEMO-P-001')
  await limitPage.getByRole('button', { name: 'Отправить запрос', exact: true }).click()
  await limitReply.getByText('DEMO-P-001', { exact: true }).waitFor()
  await limitReply.getByRole('button', { name: 'Проверить коммерческую заявку', exact: true }).click()
  await limitPage.getByText(/не более 50 уникальных позиций/).waitFor()
  assert.equal(new URL(limitPage.url()).hash, '#chat')
  await expect(limitPage.getByRole('heading', { name: 'Оформление заявки', exact: true })).toHaveCount(0)
  assert.deepEqual(await limitPage.evaluate(() => JSON.parse(sessionStorage.getItem('akbrizcase.selected-items'))), config.limit_selection)
  assert.equal(orders, 0)
  assert.equal(errors, 0)
  await limitPage.close()
  process.stdout.write(JSON.stringify({ knowledge: true, exactVersion: true, commerce: true, diagnosis: true, reload: true, noOrders: true, localStorageEmpty: true, mobile: true, commercialNavigation: true, backForward: true, requestLimit50: true }) + '\n')
} finally {
  await context.close()
  await browser.close()
}
