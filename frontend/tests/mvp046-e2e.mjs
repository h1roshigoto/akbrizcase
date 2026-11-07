// One real HTTPS system and linked write set. No fabricated successful response.
import assert from 'node:assert/strict'
import { createInterface } from 'node:readline'
import { createHash, randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { chromium, expect } from '@playwright/test'
import { staffLogin } from './staff-login.mjs'

const input = createInterface({ input: process.stdin })
const lines = input[Symbol.asyncIterator]()
const read = async () => JSON.parse((await lines.next()).value)
const config = await read()
const rpc = async (action, data = {}) => {
  process.stdout.write(JSON.stringify({ action, ...data }) + '\n')
  return read()
}
const checkpoint = async name => rpc('checkpoint', { case: name })
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined })
const contexts = [], secrets = [], consoles = [], pageErrors = [], knowledge = [], commercialReplays = []
const ordersPath = '/api/v1/orders'
let stage = 'setup'
const safe = (condition, message) => assert(condition, message)
const current = page => page.getByRole('article', { name: 'Актуальный ответ', exact: true })
const send = page => page.getByRole('button', { name: 'Подтвердить и отправить заявку', exact: true })
const orderResponse = page => page.waitForResponse(r => new URL(r.url()).pathname === ordersPath && r.request().method() === 'POST')
const owner = page => page.evaluate(() => JSON.parse(sessionStorage.getItem('akbrizcase.chat.v1')))
async function newContext(viewport = { width: 1280, height: 800 }) {
  const context = await browser.newContext({ viewport, ignoreHTTPSErrors: false, acceptDownloads: true })
  contexts.push(context)
  context.on('page', page => {
    page.on('console', message => consoles.push(message.text()))
    page.on('pageerror', () => pageErrors.push('pageerror'))
  })
  return context
}
async function visible(page) {
  safe(!await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), 'No horizontal overflow')
  safe(await page.getByRole('main').count() === 1, 'One main region')
  for (const target of [page.getByRole('main').getByLabel('Контактное имя', { exact: false }), send(page)]) {
    if (await target.count()) {
      await target.scrollIntoViewIfNeeded()
      await expect(target).toBeInViewport()
    }
  }
}
async function fields(page, comment) {
  await page.getByRole('main').getByLabel('Контактное имя', { exact: false }).fill('SYNTHETIC046 Buyer')
  await page.getByRole('main').getByLabel('Email', { exact: true }).fill('buyer046@example.invalid')
  await page.getByRole('main').getByLabel(/Комментарий|Описание проблемы/).fill(comment)
  await page.getByRole('main').getByText(/Версия: SYNTHETIC-/).waitFor()
  await visible(page)
}
async function retained(page, comment) {
  await expect(page.getByRole('main').getByLabel('Контактное имя', { exact: false })).toHaveValue('SYNTHETIC046 Buyer')
  await expect(page.getByRole('main').getByLabel('Email', { exact: true })).toHaveValue('buyer046@example.invalid')
  await expect(page.getByRole('main').getByLabel(/Комментарий|Описание проблемы/)).toHaveValue(comment)
}
async function approve(page, support = false) {
  await page.getByRole('checkbox', { name: /Я прочитал/ }).check()
  await page.getByRole('checkbox', { name: support ? /Подтверждаю окончательное/ : /Подтверждаю показанные/ }).check()
}
async function keyboard(page, support, before, comment = null) {
  const name = page.getByRole('main').getByLabel('Контактное имя', { exact: false })
  await name.focus()
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('SYNTHETIC046 Buyer')
  await page.keyboard.press('Enter')
  safe(before() === 0, 'Enter while unconfirmed cannot submit')
  const email = page.getByRole('main').getByLabel('Email', { exact: true })
  // Reach email using actual Tab traversal, allowing the optional phone field.
  for (let i = 0; i < 5 && !await email.evaluate(el => el === document.activeElement); i++)
    await page.keyboard.press('Tab')
  safe(await email.evaluate(el => el === document.activeElement), 'Email reachable by Tab')
  await page.keyboard.press('ControlOrMeta+A')
  await page.keyboard.type('buyer046@example.invalid')
  if (support) {
    const description = page.getByRole('main').getByLabel(/Описание проблемы/)
    for (let i = 0; i < 8 && !await description.evaluate(el => el === document.activeElement); i++)
      await page.keyboard.press('Tab')
    safe(await description.evaluate(el => el === document.activeElement), 'Support description reachable by Tab')
    await page.keyboard.press('ControlOrMeta+A')
    await page.keyboard.type(comment)
  }
  const consent = page.getByRole('checkbox', { name: /Я прочитал/ })
  for (let i = 0; i < 20 && !await consent.evaluate(el => el === document.activeElement); i++)
    await page.keyboard.press('Tab')
  safe(await consent.evaluate(el => el === document.activeElement), 'Consent reachable by Tab')
  await page.keyboard.press('Space')
  await page.keyboard.press('Tab')
  const confirmation = page.getByRole('checkbox', { name: support ? /Подтверждаю окончательное/ : /Подтверждаю показанные/ })
  safe(await confirmation.evaluate(el => el === document.activeElement), 'Confirmation follows consent')
  await page.keyboard.press('Space')
  await page.keyboard.press('Shift+Tab')
  safe(await consent.evaluate(el => el === document.activeElement), 'Shift+Tab returns to consent')
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  safe(await send(page).evaluate(el => el === document.activeElement), 'Submit reachable by keyboard')
  await visible(page)
}
function capture(page) {
  const requests = []
  page.on('request', request => {
    if (new URL(request.url()).pathname === ordersPath && request.method() === 'POST') {
      const h = request.headers()
      requests.push({ body: request.postData(), headers: Object.fromEntries(['idempotency-key', 'x-request-token', 'x-chat-token', 'content-type'].filter(k => h[k]).map(k => [k, h[k]])) })
      secrets.push(h['idempotency-key'], h['x-request-token'], h['x-chat-token'], JSON.parse(request.postData()).comment)
    }
  })
  return requests
}
async function replay(context, wire, receipt) {
  const response = await context.request.post(config.origin + ordersPath, { data: wire.body, headers: wire.headers })
  safe(response.status() === 200, 'Real replay status')
  safe(JSON.stringify(await response.json()) === JSON.stringify(receipt), 'Original receipt equality')
}
async function managerOrder(manager, staff, receipt) {
  await staff.goto(config.origin + '/admin/orders')
  await staff.getByRole('link', { name: String(receipt.number), exact: true }).click()
  const id = new URL(staff.url()).pathname.split('/').at(-1)
  const response = await manager.request.get(`${config.origin}/api/v1/admin/orders/${id}`)
  safe(response.status() === 200, 'Protected detail readable')
  const detail = await response.json()
  safe(detail.number === receipt.number, 'Same receipt number')
  return detail
}
async function status(manager, staff, detail) {
  secrets.push(await staff.locator('input[name=csrf_token]').first().inputValue())
  const stale = await manager.newPage()
  await stale.goto(staff.url())
  await staff.getByLabel('Next status', { exact: true }).selectOption('in_progress')
  await staff.getByRole('button', { name: 'Change status', exact: true }).click()
  await expect(staff.locator('input[name=expected_version]')).toHaveValue('2')
  const conflict = stale.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/status'))
  await stale.getByRole('button', { name: 'Change status', exact: true }).click()
  safe((await conflict).status() === 409, 'Actual stale manager form conflict')
  await stale.getByRole('link', { name: 'Reload current order', exact: true }).waitFor()
  await stale.close()
  await rpc('status', { number: detail.number })
}
async function postForm(page, label, expected = 303) {
  secrets.push(await page.locator('input[name=csrf_token]').first().inputValue())
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.startsWith('/admin/catalog/')),
    page.waitForNavigation({ waitUntil: 'load' }),
    page.getByRole('button', { name: label, exact: true }).click(),
  ])
  safe(response.status() === expected, 'Actual SQLAdmin mutation status')
}

try {
  const manager = await newContext()
  const staff = await manager.newPage()
  await staffLogin(staff, config, 'manager')
  secrets.push(...(await manager.cookies()).map(c => c.value))
  const buyers = []
  for (const [index, name, viewport] of [[0, 'desktop', { width: 1280, height: 800 }], [1, 'mobile', { width: 390, height: 844 }]]) {
    stage = 'A.' + name
    await staff.setViewportSize(viewport)
    const context = await newContext(viewport)
    const page = await context.newPage()
    const requests = capture(page)
    buyers.push({ context, page, name })
    const root = await page.goto(config.origin)
    safe(root.status() === 200 && (await root.securityDetails()).protocol.startsWith('TLS'), 'Verified HTTPS')
    const results = page.getByRole('region', { name: 'Результаты каталога' })
    for (const sku of ['DEMO-P-001', 'DEMO-S-001'])
      await results.getByRole('article').filter({ hasText: sku }).getByRole('button', { name: 'Добавить в список', exact: true }).click()
    const selection = page.getByRole('complementary', { name: 'Выбранные позиции' })
    await selection.getByLabel('Количество DEMO-P-001', { exact: true }).fill('2')
    await selection.getByLabel('Количество DEMO-S-001', { exact: true }).fill('3')
    await page.getByRole('button', { name: 'Оформить заявку', exact: true }).click()
    await page.getByText('SYNTHETIC DEMO/TEST CONSENT PLACEHOLDER.', { exact: false }).waitFor()
    await page.getByRole('region', { name: 'Обзор позиций' }).getByText('Цена по запросу', { exact: true }).waitFor()
    safe(!await page.getByRole('region', { name: 'Обзор позиций' }).getByText('0,00 RUB', { exact: true }).count(), 'Unknown price not zero')
    const comment = 'SYNTHETIC046 commercial ' + name
    await page.getByRole('main').getByLabel(/Комментарий|Описание проблемы/).fill(comment)
    // Required name/email, two independent confirmations and submission are
    // traversed by Tab/Shift+Tab/Space/Enter on both viewports.
    await keyboard(page, false, () => requests.length)
    await page.goBack()
    await page.getByRole('heading', { name: 'Товары и услуги', exact: true }).waitFor()
    await page.goForward()
    await retained(page, comment)
    await expect(page.getByRole('checkbox', { name: /Я прочитал/ })).toBeEnabled()
    await keyboard(page, false, () => requests.length)
    await checkpoint('keyboard.' + name)
    if (index === 1) {
      stage = 'E.409'
      await rpc('consent')
      let pending = orderResponse(page)
      await page.keyboard.press('Enter')
      safe((await pending).status() === 409, 'Real consent conflict')
      await page.getByRole('button', { name: 'Пересмотреть заявку' }).click()
      await page.getByText('Версия: SYNTHETIC-DEMO-ONLY-MVP046-v2', { exact: true }).waitFor()
      await retained(page, comment)
      safe(!await page.getByRole('checkbox', { name: /Я прочитал/ }).isChecked(), 'Consent requires reacceptance')
      await approve(page)
      await rpc('price')
      pending = orderResponse(page)
      await send(page).click()
      safe((await pending).status() === 409, 'Real price conflict')
      await page.getByRole('button', { name: 'Пересмотреть заявку' }).click()
      await retained(page, comment)
      await page.getByRole('button', { name: 'Принять обновление DEMO-P-001', exact: true }).click()
      safe(!await page.getByRole('checkbox', { name: /Подтверждаю показанные/ }).isChecked(), 'Price review requires confirmation')
      safe(requests.length === 2, 'No automatic conflict resend')
      await approve(page)
      await checkpoint('E.409')
    }
    stage = 'A.commit.' + name
    let receipt
    if (index === 0) {
      // route.fetch receives the real committed 201; discard it only at the
      // client boundary. DB joins below are read before any manual retry.
      await page.route('**/api/v1/orders', async route => {
        const delivered = await route.fetch()
        safe(delivered.status() === 201, 'Actual first 201')
        receipt = await delivered.json()
        await rpc('committed', { number: receipt.number })
        await route.abort('failed')
      }, { times: 1 })
      await send(page).evaluate(button => { button.click(); button.click() })
      await page.getByText(/Результат отправки пока неизвестен/).waitFor()
      safe(requests.length === 1, 'Double click cannot duplicate POST')
      await rpc('baseline')
      const retry = orderResponse(page)
      await page.getByRole('button', { name: 'Повторить отправку', exact: true }).click()
      safe((await retry).status() === 200, 'Real manual replay after lost response')
      safe(requests[0].body === requests[1].body && JSON.stringify(requests[0].headers) === JSON.stringify(requests[1].headers), 'Exact retry body and credentials')
      await rpc('unchanged')
      await checkpoint('E.network-after')
    } else {
      const pending = orderResponse(page)
      await send(page).click()
      const response = await pending
      safe(response.status() === 201, 'Actual mixed commercial creation')
      receipt = await response.json()
      await rpc('committed', { number: receipt.number })
    }
    await page.getByRole('heading', { name: `Заявка №${receipt.number}`, exact: true }).waitFor()
    await visible(page)
    const detail = await managerOrder(manager, staff, receipt)
    safe(detail.items.length === 2 && detail.consent && detail.history.length === 1, 'Manager snapshots, consent and history')
    for (const key of ['known_subtotal', 'items_total', 'delivery_status', 'delivery_cost', 'total_amount', 'currency'])
      safe(detail[key] === receipt[key], 'Receipt equals committed server projection')
    const product = detail.items.find(i => i.sku_snapshot === 'DEMO-P-001')
    const service = detail.items.find(i => i.sku_snapshot === 'DEMO-S-001')
    safe(product.qty === 2 && product.unit_price_snapshot === (index ? '150.00' : '123.45') && service.qty === 3 && service.unit_price_snapshot === null, 'Exact quantities and prices')
    await status(manager, staff, detail)
    if (index === 0) {
      stage = 'A.notification'
      const failed = await rpc('fail')
      safe(failed.order_id === detail.id, 'Worker processes the UI-created order')
      await staff.reload()
      const notice = staff.locator('section[aria-labelledby=notification]')
      await expect(notice).toContainText('failed')
      const retried = staff.waitForResponse(r => new URL(r.url()).pathname.endsWith('/retry-notification'))
      await staff.getByRole('button', { name: 'Повторить уведомление', exact: true }).click()
      safe((await retried).status() === 303, 'Protected retry PRG')
      await expect(notice).toContainText('pending')
      await rpc('retried')
      await rpc('send')
      await staff.reload()
      await expect(notice).toContainText('sent')
      await replay(context, requests[0], receipt)
    }
    commercialReplays.push({ context, wire: requests.at(-1), receipt })
    await checkpoint('A.' + name)

    stage = 'C.' + name
    await page.getByRole('button', { name: 'Вернуться в каталог', exact: true }).click()
    // A completed receipt has no pending credentials. A real page reload starts
    // a fresh intake while ordinary selection remains in sessionStorage.
    await page.reload()
    // Keep the actual commercial selection: support must never inherit items.
    await page.getByRole('link', { name: 'Помощник', exact: true }).click()
    await current(page).waitFor()
    await page.getByRole('main').getByLabel('Модель для диагностики').selectOption(config.model_id)
    await page.getByRole('button', { name: 'Синтетическая проверка оборудования', exact: true }).click()
    await current(page).getByText('Синтетический вопрос диагностики', { exact: true }).waitFor()
    const credentials = await owner(page)
    secrets.push(credentials.session_token)
    const state = async () => (await context.request.get(`${config.origin}/api/v1/chat/sessions/${credentials.session_id}`, { headers: { 'X-Chat-Token': credentials.session_token } })).json()
    let version = (await state()).state_version
    for (const input of [{ type: 'text', value: 'Синтетический утверждённый результат' }, { type: 'button', option_id: 'result' }]) {
      const response = await context.request.post(config.origin + '/api/v1/chat/messages', { headers: { 'X-Chat-Token': credentials.session_token }, data: { session_id: credentials.session_id, request_id: randomUUID(), expected_version: version, input } })
      safe(response.status() === 200, 'Adversarial diagnostic input handled')
      const result = await response.json()
      safe(result.reply.resolution !== 'results', 'Text/forged option never skip to result')
      version = result.state_version
    }
    // Actual UI must reconcile the externally advanced version, then show only
    // the permitted current-node buttons.
    const staleMessage = page.waitForResponse(r => r.url().endsWith('/api/v1/chat/messages'))
    await current(page).getByRole('button', { name: 'Синтетическое наблюдение', exact: true }).click()
    safe((await staleMessage).status() === 409, 'Actual stale expected_version conflict in UI')
    await page.getByText(/Состояние изменилось/).waitFor()
    await expect(current(page).getByRole('button', { name: 'Синтетическое наблюдение', exact: true })).toBeEnabled()
    safe((await state()).reply.resolution === 'question', 'Conflict does not auto-advance diagnostic state')
    await current(page).getByRole('button', { name: 'Синтетическое наблюдение', exact: true }).click()
    await current(page).getByText('Синтетический утверждённый результат', { exact: true }).waitFor()
    await current(page).getByRole('button', { name: 'Помогло', exact: true }).click()
    await current(page).getByText('Диагностика завершена', { exact: true }).waitFor()
    safe((await state()).outcome === 'resolved', 'Explicit feedback persisted')
    const before = requests.length
    await current(page).getByRole('button', { name: 'Связаться со специалистом', exact: true }).click()
    const newSupport = page.getByRole('button', { name: 'Начать новое обращение из чата', exact: true })
    if (await newSupport.count()) await newSupport.click()
    await page.getByRole('heading', { name: 'Обращение к специалисту', exact: true }).waitFor()
    safe(requests.length === before, 'Handoff is review only')
    safe((await page.getByRole('main').getByLabel(/Комментарий|Описание проблемы/).inputValue()).includes('Подробности опишу'), 'Safe general draft is offered')
    const commentSupport = `  SYNTHETIC046 support ${name}  `
    await fields(page, commentSupport)
    await page.goBack()
    await current(page).waitFor()
    await page.goForward()
    await retained(page, commentSupport)
    await keyboard(page, true, () => requests.length - before, commentSupport)
    const submitted = orderResponse(page)
    await page.keyboard.press('Enter')
    const accepted = await submitted
    safe(accepted.status() === 201, 'Actual support 201')
    const supportReceipt = await accepted.json()
    await page.getByRole('heading', { name: `Заявка №${supportReceipt.number}`, exact: true }).waitFor()
    await rpc('committed', { number: supportReceipt.number })
    const supportWire = requests.at(-1)
    const supportBody = JSON.parse(supportWire.body)
    safe(supportBody.kind === 'support' && supportBody.items.length === 0 && supportBody.comment === commentSupport, 'Exact support without commercial items')
    let supportDetail = await managerOrder(manager, staff, supportReceipt)
    safe(supportDetail.comment === commentSupport && supportDetail.notification_status === 'pending', 'Manager exact comment before cleanup')
    await rpc('delete_chat', { session_id: credentials.session_id, number: supportReceipt.number })
    await staff.reload()
    safe((await staff.locator('main').textContent()).includes(commentSupport), 'SQLAdmin preserves exact comment after chat cleanup')
    supportDetail = await managerOrder(manager, staff, supportReceipt)
    safe(supportDetail.comment === commentSupport && supportDetail.history.length === 1, 'Independent comment/history after SET NULL')
    await replay(context, supportWire, supportReceipt)
    await visible(page)
    await checkpoint('C.' + name)

    stage = 'B.' + name
    // Independent knowledge tab, same marked system and document used by the
    // diagnosis above. Retain it for revoke/history/replay checks after D.
    const kb = await context.newPage()
    await kb.goto(config.origin + '/#chat')
    await current(kb).waitFor()
    await kb.getByRole('button', { name: 'Документация', exact: true }).click()
    await kb.getByLabel('Запрос по документации').fill('Синтетическийквантопылесос')
    const answer = kb.waitForResponse(r => r.url().endsWith('/api/v1/chat/messages'))
    await kb.getByRole('button', { name: 'Отправить запрос', exact: true }).click()
    const replyResponse = await answer
    safe(replyResponse.status() === 200, 'Real knowledge response')
    const wire = replyResponse.request()
    const answerBody = await replyResponse.json()
    stage = 'B.citation.' + name
    await expect(current(kb).locator('.chat-text')).toHaveText('Цитаты из опубликованных материалов (не диагноз):\n\n[1] Синтетическийквантопылесос: точная цитата для проверки.')
    const link = current(kb).getByRole('link').first()
    const url = `/api/v1/documents/${config.document_id}/versions/${config.version_id}/file`
    safe(await link.getAttribute('href') === url, 'Citation binds exact document/version')
    stage = 'B.file-http.' + name
    const file = await context.request.get(config.origin + url)
    stage = 'B.file-http.' + name + '.' + file.status()
    safe(file.status() === 200 && file.headers()['content-type'].startsWith('text/plain') && file.headers()['content-disposition'].startsWith('attachment') && file.headers()['x-content-type-options'] === 'nosniff', 'File delivery security headers')
    safe(createHash('sha256').update(await file.body()).digest('hex') === config.checksum, 'HTTP bytes checksum')
    stage = 'B.download.' + name
    const downloadEvent = kb.waitForEvent('download')
    await link.click()
    const download = await downloadEvent
    const bytes = await readFile(await download.path())
    safe(createHash('sha256').update(bytes).digest('hex') === config.checksum, 'Actual browser downloaded exact file bytes')
    stage = 'B.internal-acl.' + name
    const privateUrl = `/api/v1/documents/${config.internal_document_id}/versions/${config.internal_version_id}/file`
    safe((await context.request.get(config.origin + privateUrl)).status() === 404, 'Internal file denied')
    const privateSearch = await (await context.request.get(config.origin + '/api/v1/knowledge/search?q=SYNTHETIC046-INTERNAL-NEVER-PUBLIC')).json()
    safe(privateSearch.total === 0 && privateSearch.items.length === 0, 'ACL excludes matching internal excerpt before total')
    const documentList = await (await context.request.get(config.origin + '/api/v1/documents')).json()
    safe(documentList.total === 1 && !JSON.stringify(documentList).includes(config.internal_document_id), 'Public document list excludes internal source')

    safe(!JSON.stringify(answerBody).includes(config.internal_document_id) && !(await kb.content()).includes('SYNTHETIC046-INTERNAL-NEVER-PUBLIC'), 'No internal knowledge disclosure')
    await visible(kb)
    knowledge.push({ context, page: kb, url, wire: { body: wire.postData(), token: wire.headers()['x-chat-token'] }, name })
    secrets.push(wire.headers()['x-chat-token'])
  }
  stage = 'E.429'
  const quotaContext = await newContext()
  const quotaPage = await quotaContext.newPage()
  await quotaPage.goto(config.origin + '/#chat')
  await current(quotaPage).waitFor()
  await quotaPage.getByRole('button', { name: 'Документация', exact: true }).click()
  await expect(quotaPage.getByLabel('Запрос по документации')).toBeEnabled()
  const limited = { context: quotaContext, page: quotaPage }
  const own = await owner(limited.page)
  let state = await (await limited.context.request.get(`${config.origin}/api/v1/chat/sessions/${own.session_id}`, { headers: { 'X-Chat-Token': own.session_token } })).json()
  // Fill this actual session's 30/minute quota through HTTPS, no configuration
  // switch or forged client IP. Bound the number of calls, never retry-to-PASS.
  let throttled
  for (let i = 0; i <= 30; i++) {
    // Bounded client pacing under the unchanged 120/minute ingress budget.
    // Fresh session keeps this whole message window below its fixed 60 seconds.
    await new Promise(resolve => setTimeout(resolve, 650))
    const response = await limited.context.request.post(config.origin + '/api/v1/chat/messages', { headers: { 'X-Chat-Token': own.session_token, 'X-Forwarded-For': '198.51.100.46' }, data: { session_id: own.session_id, request_id: randomUUID(), expected_version: state.state_version, input: { type: 'select_mode', mode: 'knowledge' } } })
    if (response.status() === 429) { throttled = response; break }
    safe(response.status() === 200, 'Quota fill is a genuine committed message')
    state = await response.json()
  }
  safe(throttled && Number(throttled.headers()['retry-after']) >= 1 && Number(throttled.headers()['retry-after']) <= 60 && throttled.headers()['cache-control'] === 'no-store', 'Real 429 and Retry-After')
  safe((await throttled.json()).error.code === 'RATE_LIMITED', 'Actual quota envelope')
  await rpc('quota_saturated', { session_id: own.session_id })
  const refresh = limited.page.waitForResponse(r => r.request().method() === 'GET' && new URL(r.url()).pathname === `/api/v1/chat/sessions/${own.session_id}`)
  await limited.page.getByRole('button', { name: 'Перечитать сессию', exact: true }).click()
  const refreshed = await refresh
  stage = 'E.429.refresh.' + refreshed.status()
  safe(refreshed.status() === 200, 'Current session read remains available')
  await expect(limited.page.getByRole('main').getByLabel('Запрос по документации')).toBeEnabled()
  await limited.page.getByRole('main').getByLabel('Запрос по документации').fill('SYNTHETIC046 preserved rate query')
  let attempts = 0
  limited.page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/v1/chat/messages')) attempts++ })
  const rateResponse = limited.page.waitForResponse(r => r.url().endsWith('/api/v1/chat/messages'))
  await limited.page.getByRole('button', { name: 'Отправить запрос', exact: true }).click()
  safe((await rateResponse).status() === 429, 'UI sees actual quota rejection')
  await limited.page.getByText(/Слишком много запросов/).waitFor()
  await expect(limited.page.getByRole('main').getByLabel('Запрос по документации')).toHaveValue('SYNTHETIC046 preserved rate query')
  safe(attempts === 1, 'No automatic retries')
  await rpc('recover_message_limit', { session_id: own.session_id })
  const recovered = limited.page.waitForResponse(r => r.url().endsWith('/api/v1/chat/messages'))
  await limited.page.getByRole('button', { name: 'Отправить запрос', exact: true }).click()
  safe((await recovered).status() === 200 && attempts === 2, 'Explicit safe continuation after quota recovery')
  await checkpoint('E.429')

  stage = 'D.applicability'
  await rpc('baseline')
  const admin = await newContext()
  const edit = await admin.newPage()
  await staffLogin(edit, config, 'admin')
  secrets.push(...(await admin.cookies()).map(c => c.value))
  const catalog = await (await admin.request.get(config.origin + '/api/v1/catalog/items')).json()
  const items = ['DEMO-P-001', 'DEMO-S-001'].map(sku => catalog.items.find(item => item.sku === sku))
  safe(items.every(Boolean), 'Both ordered positions still published')
  const product = items[0]
  const path = `/admin/catalog/items/${product.id}`
  await edit.goto(config.origin + path + '/applicability')
  await edit.getByLabel('Equipment model UUID', { exact: true }).fill(config.model_id)
  await edit.getByLabel('Exact evidence section UUID', { exact: true }).fill(config.internal_section_id)
  await postForm(edit, 'Create candidate')
  const review = edit.locator('form[action$="/review"]')
  await review.getByLabel('Expert name', { exact: true }).fill('SYNTHETIC ONLY Expert')
  await review.getByLabel('Review reference', { exact: true }).fill('SYNTHETIC:046:APPLICABILITY')
  await review.getByLabel('Expert approval time', { exact: false }).fill(new Date().toISOString())
  await review.getByRole('checkbox').check()
  await postForm(edit, 'Record expert review')
  const beforePermission = await (await admin.request.get(`${config.origin}/api/v1/catalog/items/${product.id}?model_id=${config.model_id}`)).json()
  safe(beforePermission.applicability.status === 'unknown', 'Review alone is insufficient')
  const publication = edit.locator('form[action$="/publish"]')
  await publication.getByRole('checkbox', { name: 'Expert has checked this exact applicability', exact: true }).check()
  await publication.getByLabel('Publication approval reference', { exact: true }).fill('SYNTHETIC:046:PUBLIC-FACT')
  await publication.getByRole('checkbox', { name: 'Allow public disclosure of this applicability fact', exact: true }).check()
  await postForm(edit, 'Allow fact publication')
  await rpc('fact_published')
  for (const buyer of buyers) {
    await buyer.page.goto(config.origin)
    const filteredResponse = buyer.page.waitForResponse(r => {
      const url = new URL(r.url())
      return url.pathname === '/api/v1/catalog/items' && url.searchParams.get('model_id') === config.model_id
    })
    await buyer.page.getByRole('main').getByLabel('Модель оборудования', { exact: true }).selectOption(config.model_id)
    const filtered = await filteredResponse
    stage = 'D.catalog-http.' + buyer.name + '.' + filtered.status()
    safe(filtered.status() === 200, 'Actual model-filtered catalog response')
    const filteredBody = await filtered.json()
    stage = 'D.catalog-total.' + buyer.name + '.' + (Number.isInteger(filteredBody.total) ? filteredBody.total : 'invalid')
    safe(filteredBody.total === 1 && filteredBody.items.length === 1, 'One committed confirmed result')
    const results = buyer.page.getByRole('region', { name: 'Результаты каталога' })
    await expect(results.getByRole('article')).toHaveCount(1)
    await results.getByText('Применимость подтверждена', { exact: true }).waitFor()
    const response = await buyer.context.request.get(`${config.origin}/api/v1/catalog/items?model_id=${config.model_id}`)
    const result = await response.json()
    safe(result.total === 1 && result.items[0].applicability.status === 'confirmed' && result.items[0].applicability.sources.length === 0, 'Public fact, zero internal sources')
    for (const forbidden of [config.internal_document_id, config.internal_version_id, config.internal_section_id, 'SYNTHETIC046-PRIVATE-TITLE', 'SYNTHETIC046-INTERNAL-NEVER-PUBLIC'])
      safe(!JSON.stringify(result).includes(forbidden) && !(await buyer.page.content()).includes(forbidden), 'Internal evidence does not leak')
    await results.getByRole('article').getByRole('button', { name: product.name, exact: true }).click()
    await buyer.page.getByRole('region', { name: 'Карточка позиции' }).getByText('Применимость подтверждена', { exact: true }).waitFor()
    await visible(buyer.page)
  }
  await rpc('revoke_internal')
  for (const buyer of buyers) {
    await buyer.page.reload()
    await buyer.page.getByRole('main').getByLabel('Модель оборудования', { exact: true }).selectOption(config.model_id)
    await expect(buyer.page.getByRole('region', { name: 'Результаты каталога' }).getByRole('article')).toHaveCount(0)
    const result = await (await buyer.context.request.get(`${config.origin}/api/v1/catalog/items/${product.id}?model_id=${config.model_id}`)).json()
    safe(result.applicability.status === 'unknown' && result.applicability.sources.length === 0, 'Revoked evidence invalidates applicability')
    safe((await (await buyer.context.request.get(`${config.origin}/api/v1/catalog/items?model_id=${config.model_id}`)).json()).total === 0, 'Filter total after revoke')
  }
  await rpc('unchanged')
  await checkpoint('D.applicability')

  stage = 'D.images'
  const images = new Map()
  async function publicImages(expected) {
    for (const buyer of buyers) {
      await buyer.page.goto(config.origin)
      for (const item of items) {
        await buyer.page.getByRole('searchbox').fill(item.sku)
        await buyer.page.getByRole('button', { name: 'Найти', exact: true }).click()
        const card = buyer.page.getByRole('region', { name: 'Результаты каталога' }).getByRole('article').filter({ hasText: item.sku })
        if (expected) {
          await expect(card.locator('img')).toHaveAttribute('src', images.get(item.id))
          await expect.poll(() => card.locator('img').evaluate(el => el.complete && el.naturalWidth > 0)).toBe(true)
          await card.getByRole('button', { name: item.name, exact: true }).click()
          await expect(buyer.page.locator('.item-detail > .item-image img')).toHaveAttribute('src', images.get(item.id))
        } else {
          await expect(card.getByRole('img', { name: 'Изображение отсутствует', exact: true })).toBeVisible()
          await card.getByRole('button', { name: item.name, exact: true }).click()
          await expect(buyer.page.getByRole('region', { name: 'Карточка позиции' }).getByRole('img', { name: 'Изображение отсутствует', exact: true })).toBeVisible()
        }
        await visible(buyer.page)
      }
    }
  }
  for (const [index, item] of items.entries()) {
    await edit.goto(`${config.origin}/admin/catalog/items/${item.id}/image`)
    await edit.getByLabel('Image file', { exact: true }).setInputFiles(index ? 'frontend/tests/fixtures/mvp026-service.png' : 'frontend/tests/fixtures/mvp026-product.jpg')
    await postForm(edit, 'Upload image')
    images.set(item.id, (await (await admin.request.get(`${config.origin}/api/v1/catalog/items/${item.id}`)).json()).image_url)
  }
  await publicImages(true)
  const old = images.get(product.id)
  await edit.goto(config.origin + path + '/image')
  await edit.getByLabel('Image file', { exact: true }).setInputFiles('frontend/tests/fixtures/mvp026-service.png')
  await postForm(edit, 'Replace image')
  const replacement = (await (await admin.request.get(`${config.origin}/api/v1/catalog/items/${product.id}`)).json()).image_url
  safe(replacement !== old && (await buyers[0].context.request.get(config.origin + old)).status() === 404, 'Old image key invalidated')
  images.set(product.id, replacement)
  await edit.getByLabel('Image file', { exact: true }).setInputFiles({ name: 'invalid.png', mimeType: 'image/png', buffer: Buffer.from('SYNTHETIC invalid image') })
  await postForm(edit, 'Replace image', 422)
  safe((await (await admin.request.get(`${config.origin}/api/v1/catalog/items/${product.id}`)).json()).image_url === replacement, 'Failed upload preserves valid image')
  await publicImages(true)
  for (const item of items) {
    await edit.goto(`${config.origin}/admin/catalog/items/${item.id}/image`)
    await postForm(edit, 'Remove image')
    safe((await buyers[0].context.request.get(config.origin + images.get(item.id))).status() === 404, 'Removed image denied')
  }
  await publicImages(false)
  // Reattach before archive, so the archive denial proves publication checking,
  // rather than merely observing the earlier removal.
  await edit.goto(config.origin + path + '/image')
  await edit.getByLabel('Image file', { exact: true }).setInputFiles('frontend/tests/fixtures/mvp026-product.jpg')
  await postForm(edit, 'Upload image')
  const archivedImage = (await (await admin.request.get(`${config.origin}/api/v1/catalog/items/${product.id}`)).json()).image_url
  await edit.goto(config.origin + path)
  await postForm(edit, 'Archive')
  for (const buyer of buyers) {
    safe((await buyer.context.request.get(`${config.origin}/api/v1/catalog/items/${product.id}`)).status() === 404, 'Archived card denied')
    safe((await buyer.context.request.get(config.origin + archivedImage)).status() === 404, 'Archived image denied')
    await buyer.page.reload()
    await buyer.page.getByRole('searchbox').fill(product.sku)
    await buyer.page.getByRole('button', { name: 'Найти', exact: true }).click()
    await expect(buyer.page.getByRole('region', { name: 'Результаты каталога' }).getByRole('article')).toHaveCount(0)
  }
  const imageState = await rpc('image_state')
  safe(imageState[0].status === 'archived' && imageState[0].image_key !== null && imageState[1].image_key === null, 'Database current image bindings and archive')
  await rpc('unchanged')
  for (const original of commercialReplays) await replay(original.context, original.wire, original.receipt)
  await rpc('unchanged')
  await checkpoint('D.images')

  stage = 'B.withdrawal'
  await rpc('revoke_public')
  for (const item of knowledge) {
    await item.page.getByRole('button', { name: 'Перечитать сессию', exact: true }).click()
    await expect(current(item.page).getByRole('link')).toHaveCount(0)
    await item.page.reload()
    await current(item.page).waitFor()
    safe((await item.context.request.get(config.origin + item.url)).status() === 404, 'Old exact file URL denied')
    const response = await item.context.request.post(config.origin + '/api/v1/chat/messages', { data: item.wire.body, headers: { 'Content-Type': 'application/json', 'X-Chat-Token': item.wire.token } })
    safe(response.status() === 200, 'Real stored message replay')
    const projected = JSON.stringify(await response.json())
    safe(!projected.includes('точная цитата для проверки') && !projected.includes(item.url), 'Replay reprojects persisted JSONB')
    const credentials = await owner(item.page)
    const history = await item.context.request.get(`${config.origin}/api/v1/chat/sessions/${credentials.session_id}`, { headers: { 'X-Chat-Token': credentials.session_token } })
    const projectedHistory = JSON.stringify(await history.json())
    safe(!projectedHistory.includes('точная цитата для проверки') && !projectedHistory.includes(item.url), 'History excludes withdrawn content')
    safe(!(await item.page.content()).includes('точная цитата для проверки'), 'Old citation absent in DOM')
    await checkpoint('B.' + item.name)
  }
  await rpc('unchanged')

  stage = 'E.410'
  const expiry = await newContext({ width: 390, height: 844 })
  const page = await expiry.newPage()
  const requests = capture(page)
  await page.goto(config.origin + '/#chat')
  await current(page).waitFor()
  await current(page).getByRole('button', { name: 'Связаться со специалистом', exact: true }).click()
  await fields(page, '  SYNTHETIC046 independent expiry  ')
  const session = await owner(page)
  secrets.push(session.session_token)
  await rpc('expire', { session_id: session.session_id })
  await approve(page, true)
  const denied = orderResponse(page)
  await send(page).click()
  safe((await denied).status() === 410, 'Expired own session gives 410')
  await page.getByText(/Срок вашей сессии истёк/).waitFor()
  await page.getByRole('button', { name: 'Оформить независимо от чата', exact: true }).click()
  await retained(page, '  SYNTHETIC046 independent expiry  ')
  safe(!await page.getByRole('checkbox', { name: /Я прочитал/ }).isChecked(), 'Fallback resets consent')
  safe(!await page.getByRole('checkbox', { name: /Подтверждаю окончательное/ }).isChecked(), 'Fallback resets exact comment confirmation')
  safe(requests.length === 1, 'Fallback is review, no automatic POST')
  await checkpoint('E.410')
  stage = 'E.network-before'
  await approve(page, true)
  await page.route('**/api/v1/orders', route => route.abort('failed'), { times: 1 })
  await send(page).click()
  await page.getByText(/Результат отправки пока неизвестен/).waitFor()
  await rpc('unchanged')
  const accepted = orderResponse(page)
  await page.getByRole('button', { name: 'Повторить отправку', exact: true }).click()
  const response = await accepted
  safe(response.status() === 201, 'Retry after pre-send disconnect creates once')
  const receipt = await response.json()
  await page.getByRole('heading', { name: `Заявка №${receipt.number}`, exact: true }).waitFor()
  await rpc('committed', { number: receipt.number })
  safe(requests[1].body === requests[2].body && JSON.stringify(requests[1].headers) === JSON.stringify(requests[2].headers), 'Precommit retry preserves exact identity')
  const siteBody = JSON.parse(requests[2].body)
  safe(siteBody.source === 'site' && !siteBody.chat_session_id && !requests[2].headers['x-chat-token'], 'Site request has no chat linkage or header')
  for (const key of ['idempotency-key', 'x-request-token']) safe(requests[0].headers[key] !== requests[2].headers[key], 'Explicit site fallback has new credentials')
  await replay(expiry, requests[2], receipt)
  await visible(page)
  await checkpoint('E.network-before')
  safe(pageErrors.length === 0, 'No uncaught browser errors')
  for (const value of [...secrets.filter(Boolean), 'buyer046@example.invalid', 'SYNTHETIC046 Buyer'])
    safe(!consoles.join('\n').includes(value), 'No contacts/secrets in browser console')
  for (const context of contexts)
    for (const tab of context.pages())
      safe(await tab.evaluate(() => localStorage.length) === 0, 'No persistent browser storage')
  process.stdout.write(JSON.stringify({ action: 'done', proof: { result: 'PASS: MVP-046 linked browser acceptance A–E, desktop/mobile/keyboard, real TLS/HTTP/PostgreSQL', secrets: [...secrets.filter(Boolean), 'buyer046@example.invalid', 'SYNTHETIC046 Buyer'] } }) + '\n')
} catch (error) {
  // Closed stage names only; Playwright errors can include contacts and HTML.
  const location = [...(error?.stack?.matchAll(/mvp046-e2e\.mjs:(\d+):\d+/g) ?? [])].at(-1)?.[1] ?? 'unknown'
  process.stderr.write('MVP046_BROWSER_FAILED stage=' + stage + ' line=' + location + '\n')
  process.exitCode = 1
} finally {
  for (const context of contexts) await context.close()
  await browser.close()
  input.close()
  process.stdin.destroy()
}
