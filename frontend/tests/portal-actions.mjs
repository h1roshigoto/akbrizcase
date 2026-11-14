import { expect } from '@playwright/test'

export async function navigate(page, name) {
  const link = page.locator('.hdr').getByRole('link', { name, exact: true })
  const menu=page.getByRole('button',{name:'Меню',exact:true})
  if (await menu.isVisible() && await menu.getAttribute('aria-expanded') !== 'true') await menu.click()
  await link.click()
}
export async function openCart(page) {
  const dialog = page.getByRole('dialog', { name: 'Состав заявки' })
  if (!await dialog.isVisible()) await page.getByRole('button', { name: /Открыть корзину/ }).click()
  await expect(dialog).toBeVisible()
  return dialog
}
export async function closeCart(page) {
  const dialog = page.getByRole('dialog', { name: 'Состав заявки' })
  if (await dialog.isVisible()) await dialog.getByRole('button', { name: 'Закрыть корзину', exact: true }).click()
}
export async function openRequest(page) {
  const dialog = page.getByRole('dialog', { name: 'Состав заявки' })
  if (await dialog.isVisible()) await dialog.getByRole('button', { name: 'Оформить заявку', exact: true }).click()
  else await navigate(page, 'Заявка')
}
export async function selectDiagnosisModel(page, id) {
  const response = await page.request.get(new URL('/api/v1/equipment-models?limit=100', page.url()).href)
  expect(response.status()).toBe(200)
  const model = (await response.json()).items.find(row => row.id === id)
  expect(model).toBeTruthy()
  await page.getByRole('main').getByRole('button', {name: model.name, exact: true}).click()
}
export async function selectCatalogModel(page, id) {
  const response = await page.request.get(new URL('/api/v1/equipment-models?limit=100', page.url()).href)
  expect(response.status()).toBe(200)
  const model = (await response.json()).items.find(row => row.id === id)
  expect(model).toBeTruthy()
  const radio=page.getByRole('radio',{name:model.name,exact:true})
  await radio.locator('..').click()
  await expect(radio).toBeChecked()
}
export async function knowledgeMode(page) {
  const result = page.waitForResponse(async response => {
    if (!response.url().endsWith('/api/v1/chat/messages') || response.request().method() !== 'POST' || response.status() !== 200) return false
    return JSON.parse(response.request().postData()).input.type === 'text' && (await response.json()).reply.mode === 'knowledge'
  })
  await page.getByRole('button', {name:'Индикатор «Готов»', exact:true}).click()
  await result
  await expect(page.getByLabel('Сообщение', {exact:true})).toBeEnabled()
}
export async function commerceMode(page) {
  const result = page.waitForResponse(async response => response.url().endsWith('/api/v1/chat/messages') && response.status() === 200 && (await response.json()).reply.mode === 'commerce')
  await page.getByRole('button', {name:'Нужна запчасть', exact:true}).click()
  await result
}
export async function diagnosisMode(page) {
  await page.getByRole('tab', {name:'Подбор по шагам', exact:true}).click()
  await expect(page.getByRole('heading', {name:'Какая модификация у изделия?',exact:true})).toBeVisible()
}
export async function refreshSession(page) {
  await page.reload()
  await page.getByRole('article', {name:'Актуальный ответ',exact:true}).waitFor()
}
