// Real trusted HTTPS support intake. No fixtures replace API responses.
import assert from 'node:assert/strict'
import { createInterface } from 'node:readline'
import { chromium, expect } from '@playwright/test'
import { staffLogin } from './staff-login.mjs'
const inputLines = createInterface({ input: process.stdin })
const lines = inputLines[Symbol.asyncIterator]()
const read = async () => JSON.parse((await lines.next()).value)
const config = await read()
const rpc = async (action, data) => {
  process.stdout.write(JSON.stringify({ action, ...data }) + '\n')
  return read()
}
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined })
const secrets = []
const exact = '  SYNTHETIC-036 confirmed <script>literal</script>  '
let step = 'diagnosis'
const progress = stage => { step = stage; process.stdout.write(JSON.stringify({ action: 'progress', stage }) + '\n') }
let posts = 0
let errors = 0
const contexts = []
async function customer() {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, ignoreHTTPSErrors: false })
  contexts.push(context)
  const page = await context.newPage()
  const requests = []
  page.on('pageerror', () => errors++)
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/v1/orders' && request.method() === 'POST') {
      posts++
      const headers = request.headers()
      requests.push({ body: request.postData(), headers })
      secrets.push(headers['x-request-token'], headers['idempotency-key'])
      if (headers['x-chat-token']) secrets.push(headers['x-chat-token'])
    }
  })
  await page.goto(config.origin + '/#chat')
  await page.getByRole('article', { name: 'Актуальный ответ', exact: true }).waitFor()
  return { context, page, requests }
}
const current = page => page.getByRole('article', { name: 'Актуальный ответ', exact: true })
const form = page => page.getByRole('main')
async function openSupport(page) {
  await current(page).getByRole('button', { name: 'Связаться со специалистом', exact: true }).click()
  await page.getByRole('heading', { name: 'Обращение к специалисту', exact: true }).waitFor()
}
async function fill(page, text) {
  await form(page).getByLabel('Контактное имя', { exact: false }).fill('Synthetic Support Buyer')
  await form(page).getByLabel('Email', { exact: true }).fill('support036@example.invalid')
  await form(page).getByLabel(/Комментарий|Описание проблемы/).fill(text)
  await form(page).getByText(/Версия: SYNTHETIC-/).waitFor()
}
async function approve(page) {
  await form(page).getByRole('checkbox', { name: /Я прочитал/ }).check()
  await form(page).getByRole('checkbox', { name: /Подтверждаю окончательное/ }).check()
}
const send = page => form(page).getByRole('button', { name: 'Подтвердить и отправить заявку', exact: true })
const response = page => page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/orders' && r.request().method() === 'POST')
const sessionId = page => page.evaluate(() => JSON.parse(sessionStorage.getItem('akbrizcase.chat.v1')).session_id)
try {
  const first = await customer()
  const { page } = first
  progress('select published model')
  await page.getByLabel('Модель для диагностики').selectOption(config.model_id)
  await page.getByRole('button', { name: 'Синтетическая проверка оборудования', exact: true }).click()
  await current(page).getByText('Синтетический вопрос диагностики', { exact: true }).waitFor()
  await page.getByRole('button', { name: 'Синтетическое наблюдение', exact: true }).click()
  await current(page).getByText('Синтетический утверждённый результат', { exact: true }).waitFor()
  progress('open handoff form')
  await openSupport(page)
  assert.equal(posts, 0, 'Opening support must not send orders')
  assert((await form(page).getByLabel(/Комментарий|Описание проблемы/).inputValue()).includes('Подробности опишу'), 'General server draft is proposed')
  progress('edit and navigate support')
  await fill(page, exact)
  await page.goBack()
  await current(page).waitFor()
  await page.goForward()
  await page.getByRole('heading', { name: 'Обращение к специалисту', exact: true }).waitFor()
  assert((await form(page).getByLabel(/Комментарий|Описание проблемы/).inputValue()) === exact, 'Back/Forward keeps edited description')
  progress('confirm and submit support')
  await approve(page)
  const pending = response(page)
  await send(page).dblclick()
  const accepted = await pending
  assert.equal(accepted.status(), 201)
  const receipt = await accepted.json()
  await page.getByRole('heading', { name: `Заявка №${receipt.number}`, exact: true }).waitFor()
  assert.equal(first.requests.length, 1, 'Double click sends one POST')
  assert(JSON.parse(first.requests[0].body).comment === exact, 'Exact confirmed text on wire')
  const identifier = await sessionId(page)
  await rpc('delete_chat', { session_id: identifier, number: receipt.number })
  progress('authenticated replay after deletion')
  const replay = await first.context.request.post(config.origin + '/api/v1/orders', { data: first.requests[0].body, headers: first.requests[0].headers })
  assert.equal(replay.status(), 200)
  assert(JSON.stringify(await replay.json()) === JSON.stringify(receipt), 'Same original safe receipt')
  progress('manager after deletion')
  const manager = await browser.newContext({ ignoreHTTPSErrors: false })
  contexts.push(manager)
  const staff = await manager.newPage()
  await staffLogin(staff, config, 'manager')
  secrets.push(...(await manager.cookies()).map(cookie => cookie.value))
  await staff.getByRole('link', { name: 'Orders', exact: true }).click()
  await staff.getByRole('link', { name: String(receipt.number), exact: true }).click()
  assert((await staff.locator('main').textContent()).includes(exact), 'Manager HTML preserves exact comment')
  assert.equal(await staff.locator('main script').count(), 0)
  const list = await (await manager.request.get(config.origin + '/api/v1/admin/orders')).json()
  const detail = await (await manager.request.get(config.origin + '/api/v1/admin/orders/' + list.items[0].id)).json()
  assert(detail.comment === exact, 'Protected detail preserves exact comment after FK SET NULL')
  assert(detail.notification_status === 'pending', 'SMTP is independent')
  progress('expired own session fallback')
  const fallback = await customer()
  await openSupport(fallback.page)
  await fill(fallback.page, 'SYNTHETIC-036 fallback exact')
  await rpc('expire', { session_id: await sessionId(fallback.page) })
  await approve(fallback.page)
  const denied = response(fallback.page)
  await send(fallback.page).click()
  assert.equal((await denied).status(), 410)
  await fallback.page.getByText(/Срок вашей сессии истёк/).waitFor()
  assert.equal(fallback.requests.length, 1)
  await fallback.page.getByRole('button', { name: 'Оформить независимо от чата', exact: true }).click()
  assert((await form(fallback.page).getByLabel(/Комментарий|Описание проблемы/).inputValue()) === 'SYNTHETIC-036 fallback exact', 'Fallback keeps edits')
  assert.equal(fallback.requests.length, 1, 'Review does not send automatically')
  await approve(fallback.page)
  const site = response(fallback.page)
  await send(fallback.page).click()
  assert.equal((await site).status(), 201)
  await fallback.page.getByText('ЗАЯВКА ПОЛУЧЕНА', { exact: true }).waitFor()
  const [chatRequest, siteRequest] = fallback.requests
  const siteBody = JSON.parse(siteRequest.body)
  assert(siteBody.source === 'site' && !('chat_session_id' in siteBody), 'Site body has no chat linkage')
  assert(!siteRequest.headers['x-chat-token'], 'Site headers have no owner token')
  assert(siteRequest.headers['idempotency-key'] !== chatRequest.headers['idempotency-key'], 'New key after explicit fallback')
  assert(siteRequest.headers['x-request-token'] !== chatRequest.headers['x-request-token'], 'New request secret after explicit fallback')
  progress('lost committed response then expiry')
  const lost = await customer()
  await openSupport(lost.page)
  await fill(lost.page, 'SYNTHETIC-036 lost response')
  let committedReceipt
  await lost.page.route('**/api/v1/orders', async route => {
    const delivered = await route.fetch()
    assert.equal(delivered.status(), 201)
    committedReceipt = await delivered.json()
    await rpc('expire', { session_id: await sessionId(lost.page) })
    await route.abort('failed')
  }, { times: 1 })
  await approve(lost.page)
  await send(lost.page).click()
  await lost.page.getByText(/Результат отправки пока неизвестен/).waitFor()
  assert.equal(await lost.page.getByRole('button', { name: 'Оформить независимо от чата', exact: true }).count(), 0)
  const recovered = response(lost.page)
  await lost.page.getByRole('button', { name: 'Повторить отправку', exact: true }).click()
  const recoveredResponse = await recovered
  assert.equal(recoveredResponse.status(), 200)
  assert(JSON.stringify(await recoveredResponse.json()) === JSON.stringify(committedReceipt), 'Replay after expiry returns committed receipt')
  assert(lost.requests[0].body === lost.requests[1].body, 'Retry uses identical body')
  for (const name of ['idempotency-key', 'x-request-token', 'x-chat-token'])
    assert(lost.requests[0].headers[name] === lost.requests[1].headers[name], 'Retry keeps original secrets')
  await lost.page.getByText('ЗАЯВКА ПОЛУЧЕНА', { exact: true }).waitFor()
  assert.equal(errors, 0)
  for (const customerPage of [page, fallback.page, lost.page]) {
    assert.equal(await customerPage.evaluate(() => localStorage.length), 0)
    assert.equal(await customerPage.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  }
  process.stdout.write(JSON.stringify({ action: 'done', proof: { result: 'PASS: MVP-036 HTTPS diagnosis -> edited support -> PostgreSQL -> delete chat -> manager exact comment; 410 manual site fallback; committed replay after expiry; SMTP unavailable; mobile and Back/Forward', secrets } }) + '\n')
} catch {
  process.stderr.write('Support acceptance assertion failed at: ' + step + '\n')
  process.exitCode = 1
} finally {
  for (const context of contexts) await context.close()
  await browser.close()
  inputLines.close()
  process.stdin.destroy()
}
