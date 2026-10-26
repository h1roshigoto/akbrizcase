// Real trusted HTTPS Chromium → Nginx → FastAPI → PostgreSQL, synthetic only.
import assert from 'node:assert/strict'
import { createInterface } from 'node:readline'
import { randomBytes } from 'node:crypto'
import { chromium } from '@playwright/test'
const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]()
const read = async () => JSON.parse((await lines.next()).value)
const config = await read()
const rpc = async (action) => {
  process.stdout.write(JSON.stringify({ action }) + '\n')
  return read()
}
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
})
const sensitive = []
const synthetic = [
  'Synthetic MVP013 Buyer',
  'mvp013@example.invalid',
  'Synthetic form comment',
]
let original
let receipt
let posts = 0
let step = 'catalog'
const browserMessages = []
function observe(context) {
  context.on('page', (page) => {
    page.on('console', (message) => browserMessages.push(message.text()))
    page.on('request', (request) => {
      if (
        request.method() === 'POST' &&
        new URL(request.url()).pathname === '/api/v1/orders'
      ) {
        const h = request.headers()
        if (h['x-request-token']) sensitive.push(h['x-request-token'])
        if (h['idempotency-key']) sensitive.push(h['idempotency-key'])
      }
    })
  })
}
const progress = []
function safe(condition, message) {
  assert(condition, message)
}
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: false })
  observe(context)
  const page = await context.newPage()
  // Capture privately in memory for equality and leakage checks; never trace/HAR.
  page.on('request', (request) => {
    const url = new URL(request.url())
    safe(
      !url.search.includes('token') && !url.search.includes('example.invalid'),
      'No contacts or tokens in URL',
    )
    if (url.pathname === '/api/v1/orders' && request.method() === 'POST')
      posts++
  })
  await page.goto(config.origin)
  safe(
    await page.evaluate(() => isSecureContext),
    'Real secure context is required for Web Crypto',
  )
  const result = page.getByRole('region', { name: 'Результаты каталога' })
  for (const sku of ['DEMO-P-001', 'DEMO-S-001']) {
    const card = result.getByRole('article').filter({ hasText: sku })
    await card
      .getByRole('button', { name: 'Добавить в список', exact: true })
      .click()
  }
  const selection = page.getByRole('complementary', {
    name: 'Выбранные позиции',
  })
  await selection.getByLabel('Количество DEMO-P-001', { exact: true }).fill('2')
  await selection.getByLabel('Количество DEMO-S-001', { exact: true }).fill('3')
  const storedSelection = await page.evaluate(() =>
    JSON.parse(sessionStorage.getItem('akbrizcase.selected-items')),
  )
  async function openForm(target) {
    await target
      .getByRole('button', { name: 'Оформить заявку', exact: true })
      .click()
    await target
      .getByText('SYNTHETIC DEMO/TEST CONSENT PLACEHOLDER.', { exact: false })
      .waitFor()
    await target
      .getByLabel('Контактное имя', { exact: false })
      .fill(synthetic[0])
    await target.getByLabel('Email', { exact: true }).fill(synthetic[1])
    await target
      .getByLabel('Комментарий (необязательно)', { exact: true })
      .fill(synthetic[2])
  }
  async function approve(target) {
    await target.getByRole('checkbox', { name: /Я прочитал/ }).check()
    await target
      .getByRole('checkbox', { name: /Подтверждаю показанные/ })
      .check()
  }
  const send = (target) =>
    target.getByRole('button', {
      name: 'Подтвердить и отправить заявку',
      exact: true,
    })
  step = 'open form'
  await openForm(page)
  const overview = page.getByRole('region', { name: 'Обзор позиций' })
  await overview.getByText('DEMO-P-001', { exact: false }).waitFor()
  await overview.getByText('DEMO-S-001', { exact: false }).waitFor()
  await overview.getByText('Цена по запросу', { exact: true }).waitFor()
  await overview.getByText('246,90 RUB', { exact: true }).waitFor()
  // Deliver POST to real backend, then lose the committed 201 response.
  await page.route(
    '**/api/v1/orders',
    async (route) => {
      const request = route.request()
      const headers = await request.allHeaders()
      original = {
        body: request.postData(),
        headers: {
          'Idempotency-Key': headers['idempotency-key'],
          'X-Request-Token': headers['x-request-token'],
        },
      }
      sensitive.push(...Object.values(original.headers))
      const response = await route.fetch()
      safe(response.status() === 201, 'First POST commits with 201')
      receipt = await response.json()
      await route.abort('failed')
      await page.unroute('**/api/v1/orders')
    },
    { times: 1 },
  )
  step = 'lost response and retry'
  await approve(page)
  // Dispatch rapid browser clicks while the first real request is unresolved.
  await send(page).evaluate((button) => {
    button.click()
    button.click()
  })
  await page
    .getByRole('button', { name: 'Повторить отправку', exact: true })
    .waitFor()
  safe(posts === 1, 'Rapid double click produces one POST')
  safe(
    JSON.stringify(await rpc('counts')) === '[1,1,2,1,1]',
    'Exactly one complete write set after lost response',
  )
  const storage = await page.evaluate(() => ({
    local: { ...localStorage },
    session: { ...sessionStorage },
  }))
  safe(Object.keys(storage.local).length === 0, 'No localStorage')
  safe(
    Object.keys(storage.session).sort().join() ===
      'akbrizcase.pending-order,akbrizcase.selected-items',
    'Approved storage keys only',
  )
  safe(
    Object.keys(JSON.parse(storage.session['akbrizcase.pending-order']))
      .sort()
      .join() === 'Idempotency-Key,X-Request-Token',
    'No PII payload stored',
  )
  for (const value of synthetic)
    safe(
      !JSON.stringify(storage).includes(value),
      'No personal values in browser storage',
    )
  await page.route(
    '**/api/v1/orders',
    async (route) => {
      const request = route.request()
      const headers = await request.allHeaders()
      safe(request.postData() === original.body, 'Retry body byte equality')
      safe(
        headers['idempotency-key'] === original.headers['Idempotency-Key'] &&
          headers['x-request-token'] === original.headers['X-Request-Token'],
        'Retry original key and token',
      )
      const response = await route.fetch()
      safe(response.status() === 200, 'Retry is a genuine 200 replay')
      safe(
        JSON.stringify(await response.json()) === JSON.stringify(receipt),
        'Original safe receipt replay',
      )
      await route.fulfill({ response })
    },
    { times: 1 },
  )
  await page
    .getByRole('button', { name: 'Повторить отправку', exact: true })
    .click()
  await page
    .getByRole('heading', { name: `Заявка №${receipt.number}`, exact: true })
    .waitFor()
  safe(
    await page.evaluate(
      () => sessionStorage.getItem('akbrizcase.pending-order') === null,
    ),
    'Secret cleanup on receipt',
  )
  const receiptText = await page
    .getByRole('region', { name: 'Подтверждение заявки' })
    .innerText()
  for (const value of [...sensitive, ...synthetic])
    safe(!receiptText.includes(value), 'Receipt has no contacts or tokens')
  safe(
    JSON.stringify(await rpc('counts')) === '[1,1,2,1,1]',
    'Replay makes no writes',
  )
  progress.push(
    '201 lost after commit; one double-click POST; exact 200 retry; original receipt; one write set',
  )
  // Manager verifies the same persisted number/snapshots/evidence over trusted TLS.
  for (const value of sensitive)
    safe(
      !(await page.content()).includes(value),
      'No secret in DOM attributes or text',
    )
  step = 'manager verification'
  const manager = await browser.newContext({ ignoreHTTPSErrors: false })
  const staff = await manager.newPage()
  await staff.goto(`${config.origin}/admin/login`)
  await staff.getByLabel('Login', { exact: true }).fill('browser-manager')
  await staff
    .getByLabel('Password', { exact: true })
    .fill(config.passwords.manager)
  await staff.getByRole('button', { name: 'Log in', exact: true }).click()
  await staff.waitForURL(`${config.origin}/admin/`)
  for (const cookie of await manager.cookies()) sensitive.push(cookie.value)
  await staff.getByRole('link', { name: 'Orders', exact: true }).click()
  await staff
    .getByRole('link', { name: String(receipt.number), exact: true })
    .click()
  const detail = await staff.locator('main').innerText()
  // textContent is independent of stylesheet timing and layout whitespace.
  const detailSections = await staff.locator('main section').allTextContents()
  for (const text of [
    'DEMO-P-001',
    'DEMO-S-001',
    '246.90 RUB',
    'Requires quotation',
    'pending',
    'SYNTHETIC-DEMO-ONLY-v1',
    'request_form',
    'System',
  ])
    safe(detail.includes(text), `Manager verifies ${text}`)
  const detailResponse = await manager.request.get(
    `${config.origin}/api/v1/admin/orders`,
  )
  const staffOrders = await detailResponse.json()
  safe(
    staffOrders.total === 1 && staffOrders.items[0].number === receipt.number,
    'Manager number matches receipt',
  )
  const orderDetail = await (
    await manager.request.get(
      `${config.origin}/api/v1/admin/orders/${staffOrders.items[0].id}`,
    )
  ).json()
  safe(orderDetail.items.length === 2, 'Manager sees two snapshots')
  const productSnapshot = orderDetail.items.find(
    (item) => item.sku_snapshot === 'DEMO-P-001',
  )
  const serviceSnapshot = orderDetail.items.find(
    (item) => item.sku_snapshot === 'DEMO-S-001',
  )
  safe(
    productSnapshot?.qty === 2 &&
      productSnapshot.unit_price_snapshot === '123.45' &&
      productSnapshot.catalog_revision_snapshot === 1,
    'Original product snapshot and quantity',
  )
  safe(
    serviceSnapshot?.qty === 3 &&
      serviceSnapshot.unit_price_snapshot === null &&
      serviceSnapshot.kind_snapshot === 'service',
    'Original on-request service snapshot and quantity',
  )
  safe(
    orderDetail.consent.text_version === 'SYNTHETIC-DEMO-ONLY-v1' &&
      orderDetail.history.length === 1 &&
      orderDetail.history[0].to_status === 'new',
    'Original consent and single initial history event',
  )

  for (const key of [
    'known_subtotal',
    'items_total',
    'delivery_status',
    'delivery_cost',
    'total_amount',
    'currency',
  ])
    safe(
      orderDetail[key] === receipt[key],
      'Manager persisted amounts equal receipt',
    )
  step = 'Compose persistence and manager reload'
  safe(await rpc('restart'), 'Complete Compose stop/up succeeds')
  // Reuse the original identity after process restart; never generate a new key.
  const replay = await context.request.post(`${config.origin}/api/v1/orders`, {
    headers: { ...original.headers, 'Content-Type': 'application/json' },
    data: original.body,
  })
  safe(replay.status() === 200, 'Post-restart replay returns 200')
  safe(
    JSON.stringify(await replay.json()) === JSON.stringify(receipt),
    'Post-restart replay preserves original receipt',
  )
  // A real SQLAdmin reload uses the persisted manager session over verified TLS.
  const reloadResponse = await staff.reload()
  safe(reloadResponse.status() === 200, 'SQLAdmin survives Compose stop/up')
  await staff.getByRole('heading', { name: `Order ${receipt.number}`, exact: true }).waitFor()
  safe(
    JSON.stringify(await staff.locator('main section').allTextContents()) ===
      JSON.stringify(detailSections),
    'Manager sections retain the same snapshots, consent and history',
  )
  const persistedResponse = await manager.request.get(
    `${config.origin}/api/v1/admin/orders/${staffOrders.items[0].id}`,
  )
  safe(persistedResponse.status() === 200, 'Protected detail survives restart')
  // Do not let assertion diagnostics dump the synthetic contacts or hashes.
  safe(
    JSON.stringify(await persistedResponse.json()) === JSON.stringify(orderDetail),
    'Every allowlisted manager field survives restart unchanged',
  )
  safe(
    JSON.stringify(await rpc('counts')) === '[1,1,2,1,1]',
    'Post-restart replay and manager reads preserve exactly one write set',
  )
  progress.push(
    `MVP-014 synthetic receipt #${receipt.number}; SMTP unavailable; full Compose stop/up; all business rows unchanged; 200 replay; SQLAdmin manager reload with same snapshots/consent/history`,
  )
  // New fresh form, then effective consent and catalog are changed in PostgreSQL.
  step = 'consent and catalog conflicts'
  const conflicts = await browser.newContext({ ignoreHTTPSErrors: false })
  observe(conflicts)
  await conflicts.addInitScript(
    (rows) =>
      sessionStorage.setItem('akbrizcase.selected-items', JSON.stringify(rows)),
    storedSelection,
  )
  const changed = await conflicts.newPage()
  const conflictKeys = []
  changed.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      new URL(request.url()).pathname === '/api/v1/orders'
    )
      conflictKeys.push(request.headers()['idempotency-key'])
  })
  await changed.goto(config.origin)
  await openForm(changed)
  await approve(changed)
  await rpc('change_consent')
  let responsePromise = changed.waitForResponse(
    (r) => new URL(r.url()).pathname === '/api/v1/orders',
  )
  await send(changed).click()
  safe((await responsePromise).status() === 409, 'Real CONSENT_CHANGED')
  await changed.getByRole('button', { name: 'Пересмотреть заявку' }).click()
  await changed
    .getByText('Версия: SYNTHETIC-DEMO-ONLY-MVP013-v2', { exact: true })
    .waitFor()
  safe(
    !(await changed.getByRole('checkbox', { name: /Я прочитал/ }).isChecked()),
    'Changed consent is not auto accepted',
  )
  await approve(changed)
  await rpc('change_catalog')
  responsePromise = changed.waitForResponse(
    (r) => new URL(r.url()).pathname === '/api/v1/orders',
  )
  await send(changed).click()
  safe((await responsePromise).status() === 409, 'Real CATALOG_CHANGED')
  await changed.getByRole('button', { name: 'Пересмотреть заявку' }).click()
  await changed
    .getByRole('button', { name: 'Принять обновление DEMO-P-001', exact: true })
    .waitFor()
  safe(await send(changed).isDisabled(), 'Stale revision blocks submission')
  await changed
    .getByRole('button', { name: 'Принять обновление DEMO-P-001', exact: true })
    .click()
  await approve(changed)
  await rpc('archive_service')
  responsePromise = changed.waitForResponse(
    (r) => new URL(r.url()).pathname === '/api/v1/orders',
  )
  await send(changed).click()
  safe(
    (await responsePromise).status() === 409,
    'Archived item rejects real POST',
  )
  await changed.getByRole('button', { name: 'Пересмотреть заявку' }).click()
  await changed.getByText(/Её нельзя отправить/).waitFor()
  safe(
    await send(changed).isDisabled(),
    'Unavailable item cannot be resubmitted',
  )
  safe(
    JSON.stringify(await rpc('counts')) === '[1,1,2,1,1]',
    'Conflicts leave original write set untouched',
  )
  safe(
    conflictKeys.length === 3 &&
      new Set(conflictKeys).size === 3 &&
      !conflictKeys.includes(original.headers['Idempotency-Key']),
    'Explicit conflict review creates genuinely fresh independent identities',
  )
  await conflicts.close()
  progress.push(
    'PostgreSQL consent change → 409/reaccept; price/revision → 409/explicit accept; archive → blocked',
  )
  // Real HTTP adversarial responses: no fixture responses, no extra orders.
  for (const [mode, status] of [
    ['idempotency', 409],
    ['hidden', 404],
    ['validation', 422],
  ]) {
    step = `focused ${mode}`
    const focused = await browser.newContext({ ignoreHTTPSErrors: false })
    observe(focused)
    const current = await (
      await focused.request.get(`${config.origin}/api/v1/catalog/items`)
    ).json()
    const rows = current.items
      .filter((item) => item.sku === 'DEMO-P-001')
      .map((item) => ({ item_id: item.id, qty: 1, revision: item.revision }))
    await focused.addInitScript(
      (rows) =>
        sessionStorage.setItem(
          'akbrizcase.selected-items',
          JSON.stringify(rows),
        ),
      rows,
    )
    const target = await focused.newPage()
    await target.goto(config.origin)
    await openForm(target)
    await target.route(
      '**/api/v1/orders',
      async (route) => {
        let body = route.request().postData()
        const headers = await route.request().allHeaders()
        if (mode === 'validation') {
          const parsed = JSON.parse(body)
          parsed.contact.name = ''
          body = JSON.stringify(parsed)
        } else {
          headers['idempotency-key'] = original.headers['Idempotency-Key']
          headers['x-request-token'] =
            mode === 'hidden'
              ? randomBytes(32).toString('hex')
              : original.headers['X-Request-Token']
          if (mode === 'hidden') sensitive.push(headers['x-request-token'])
        }
        const response = await route.fetch({ postData: body, headers })
        safe(response.status() === status, `Focused real HTTP ${status}`)
        await route.fulfill({ response })
      },
      { times: 1 },
    )
    await approve(target)
    await send(target).click()
    if (mode === 'validation') {
      await target.getByRole('button', { name: 'Пересмотреть заявку' }).click()
      safe(
        (await target.getByLabel('Email', { exact: true }).inputValue()) ===
          synthetic[1],
        '422 preserves in-memory input',
      )
    } else {
      await target.getByText(/Обратитесь к менеджеру/).waitFor()
      safe(
        await send(target).isDisabled(),
        'Hidden/conflicting request cannot rotate key',
      )
    }
    await focused.close()
  }
  // Reload while unresolved: preserve headers, deliberately omit exact PII body.
  step = 'unresolved reload'
  const reload = await browser.newContext({ ignoreHTTPSErrors: false })
  await reload.addInitScript(
    ({ rows, headers }) => {
      sessionStorage.setItem('akbrizcase.selected-items', JSON.stringify(rows))
      sessionStorage.setItem(
        'akbrizcase.pending-order',
        JSON.stringify(headers),
      )
    },
    { rows: storedSelection, headers: original.headers },
  )
  const recovered = await reload.newPage()
  let reloadPosts = 0
  recovered.on('request', (r) => {
    if (r.method() === 'POST') reloadPosts++
  })
  await recovered.goto(config.origin)
  await recovered
    .getByRole('button', { name: 'Оформить заявку', exact: true })
    .click()
  await recovered.getByText(/Исход предыдущей отправки неизвестен/).waitFor()
  safe(
    (await send(recovered).isDisabled()) && reloadPosts === 0,
    'Reload never rebuilds or resends a changed request',
  )
  await reload.close()
  safe(
    JSON.stringify(await rpc('counts')) === '[1,1,2,1,1]',
    'All negative paths have no unintended extra writes',
  )
  progress.push(
    'Real backend 409/404/422 UI handling; unresolved reload blocked; no extra writes',
  )
  if (config.status_changes) {
    step = 'MVP-015 status changes'
    safe(await rpc('status_baseline'), 'MVP-014 invariants complete before status changes')
    const id = staffOrders.items[0].id
    const stale = await manager.newPage()
    step = 'MVP-015 stale form'
    await stale.goto(`${config.origin}/admin/orders/${id}`)
    safe(await stale.locator('input[name="expected_version"]').inputValue() === '1', 'Stale form holds version 1')
    step = 'MVP-015 start processing'
    await staff.getByLabel('Next status', { exact: true }).selectOption('in_progress')
    await staff.getByRole('button', { name: 'Change status', exact: true }).click()
    await staff.waitForURL(`${config.origin}/admin/orders/${id}`)
    await staff.locator('input[name="expected_version"][value="2"]').waitFor({ state: 'attached' })
    let current = await (await manager.request.get(`${config.origin}/api/v1/admin/orders/${id}`)).json()
    safe(current.status === 'in_progress' && current.version === 2 && current.history.length === 2, 'Version 2 and transition history')
    safe(await rpc('status_progress'), 'PostgreSQL progress and actor audit')
    step = 'MVP-015 reject stale form'
    const conflict = stale.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/status'))
    await stale.getByRole('button', { name: 'Change status', exact: true }).click()
    safe((await conflict).status() === 409, 'Old real browser form receives 409')
    await stale.getByRole('link', { name: 'Reload current order', exact: true }).waitFor()
    safe(await rpc('status_progress'), 'Conflict produces no history or successful audit')
    await stale.close()
    const reason = 'Synthetic closure "quoted" <script>window.statusInjected=true</script>\nProcessing complete'
    sensitive.push(reason)
    step = 'MVP-015 close processing'
    await staff.getByLabel('Next status', { exact: true }).selectOption('closed')
    await staff.getByLabel('Reason (required', { exact: false }).fill(reason)
    const closed = staff.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/status'))
    await staff.getByRole('button', { name: 'Change status', exact: true }).click()
    const closedResponse = await closed
    safe(closedResponse.status() === 303, 'Status uses Post/Redirect/Get')
    // Native HTML form submission encodes textarea line endings as CRLF.
    const submittedReason = new URLSearchParams(closedResponse.request().postData()).get('reason')
    safe(submittedReason?.replace(/\r\n/g, '\n') === reason, 'Native form carries the entered multiline reason')
    sensitive.push(submittedReason)
    await staff.getByText('This is a terminal status.', { exact: false }).waitFor()
    current = await (await manager.request.get(`${config.origin}/api/v1/admin/orders/${id}`)).json()
    safe(current.id === id && current.status === 'closed' && current.version === 3 && current.closed_at !== null && current.history.length === 3, 'Same order closed with version 3 and full history')
    safe(current.history[2].reason === submittedReason, 'Submitted reason persisted exactly')
    safe(current.history[2].changed_by === current.history[1].changed_by, 'Authenticated actor persisted')
    safe((await staff.locator('main').textContent()).includes(reason), 'Literal multiline reason displayed')
    safe(await staff.evaluate(() => window.statusInjected === undefined), 'Reason is escaped')
    safe(await staff.locator('input[name="expected_version"]').count() === 0, 'Terminal form absent')
    for (const key of ['items', 'consent', 'contact_name', 'email', 'comment', 'known_subtotal', 'items_total', 'delivery_status', 'delivery_cost', 'total_amount', 'currency'])
      safe(JSON.stringify(current[key]) === JSON.stringify(orderDetail[key]), 'Original manager snapshots and request data unchanged')
    step = 'MVP-015 database closure and replay'
    safe(await rpc('status_complete'), 'PostgreSQL closure and immutable full rows')
    const originalReplay = await context.request.post(`${config.origin}/api/v1/orders`, {
      headers: { ...original.headers, 'Content-Type': 'application/json' }, data: original.body,
    })
    safe(originalReplay.status() === 200 && JSON.stringify(await originalReplay.json()) === JSON.stringify(receipt), 'Replay after closure returns original new receipt')
    safe(await rpc('status_replayed'), 'Replay preserves status, history, audit and outbox')
    progress.push('MVP-015 manager new → in_progress → closed; stale form 409; versions 2/3; original snapshots and receipt; atomic PostgreSQL history/audit')
  }
  await manager.close()
  for (const value of [...sensitive, ...synthetic])
    safe(
      !browserMessages.join(' ').includes(value),
      'No personal values in browser console',
    )
  // Private parent consumes sensitive values solely to assert absence from logs.
  process.stdout.write(
    JSON.stringify({
      action: 'done',
      proof: {
        result: `PASS: MVP-013 ${progress.join('; ')}`,
        receipt: Object.fromEntries([
          'number', 'status', 'known_subtotal', 'items_total', 'delivery_status',
          'delivery_cost', 'total_amount', 'currency',
        ].map((key) => [key, receipt[key]])),
        secrets: [...sensitive, ...synthetic],
      },
    }) + '\n',
  )
  await context.close()
} catch (error) {
  // Playwright diagnostics can embed request headers; never forward them.
  const safeMessage =
    error?.code === 'ERR_ASSERTION' ? error.message : 'browser action failed'
  process.stderr.write(
    `MVP-013 ${step}: ${safeMessage}; completed phases ${progress.length}\n`,
  )
  process.exitCode = 1
} finally {
  await browser.close()
  process.stdin.destroy()
}
