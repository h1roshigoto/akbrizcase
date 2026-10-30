// Owner-marked synthetic HTTPS fixture; production authorization code is real.
import assert from 'node:assert/strict'
import { createInterface } from 'node:readline'
import { randomUUID } from 'node:crypto'
import { chromium, expect } from '@playwright/test'
const input = createInterface({ input: process.stdin })[Symbol.asyncIterator]()
const read = async () => JSON.parse((await input.next()).value)
const config = await read()
const rpc = async action => {
  process.stdout.write(JSON.stringify({ action }) + '\n')
  return read()
}
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined })
const secrets = []
let autoOrders = 0
try {
  for (const shutdown of ['missing_config', 'retire']) {
    const context = await browser.newContext({ ignoreHTTPSErrors: false })
    const page = await context.newPage()
    let original, committed, token, pending = true
    context.on('request', request => {
      if (new URL(request.url()).pathname === '/api/v1/orders' && request.method() === 'POST') autoOrders++
    })
    await page.route('**/api/v1/chat/messages', async route => {
      const response = await route.fetch()
      if (pending) {
        assert.equal(response.status(), 200)
        original = route.request().postDataJSON()
        token = route.request().headers()['x-chat-token']
        secrets.push(token)
        committed = await response.json()
        pending = false
        await route.abort('failed') // COMMIT succeeded; the browser lost its response.
      } else {
        assert.equal(response.status(), 200)
        assert.deepEqual(await response.json(), committed)
        await route.fulfill({ response })
      }
    })
    await page.goto(config.origin + '/#chat')
    await page.getByRole('article', { name: 'Актуальный ответ', exact: true }).waitFor()
    await page.getByRole('button', { name: 'Документация', exact: true }).click()
    await page.getByText(/Исход запроса неизвестен/).waitFor()
    await rpc(shutdown)
    const replayResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/chat/messages')
    await page.getByRole('button', { name: 'Повторить тот же запрос', exact: true }).click()
    assert.equal((await replayResponse).status(), 200)
    await page.getByRole('article', { name: 'Актуальный ответ', exact: true }).waitFor()
    const fresh = await context.request.post(config.origin + '/api/v1/chat/messages', {
      headers: { 'X-Chat-Token': token },
      data: { ...original, request_id: randomUUID(), expected_version: 1 },
    })
    assert.equal(fresh.status(), 503)
    assert.equal((await context.request.get(config.origin + '/api/v1/consent-text')).status(), 503)
    const policyResponse = await context.request.get(config.origin + '/api/v1/privacy-policy')
    assert.equal(policyResponse.status(), 200)
    const policy = await policyResponse.json()
    assert.equal(policy.collection_enabled, false)
    assert.equal(policy.synthetic, false)
    const document = await context.newPage()
    await document.goto(config.origin + '/privacy-policy')
    await expect(document.getByText(policy.text, { exact: true })).toBeVisible()
    await expect(document.getByText(policy.withdrawal_instructions, { exact: true })).toBeVisible()
    await expect(document.getByText(/Приём новых обращений закрыт/)).toBeVisible()
    await document.goto(config.origin + '/#support')
    await expect(document.getByRole('button', { name: 'Подтвердить и отправить заявку' })).toBeDisabled()
    await expect(document.getByRole('checkbox', { name: /Я прочитал/ })).not.toBeChecked()
    assert.equal(autoOrders, 0)
    await context.close()
    if (shutdown === 'missing_config') await rpc('restore_config')
  }
  process.stdout.write(JSON.stringify({ action: 'done', proof: { secrets,
    result: 'PASS: verified HTTPS COMMIT/lost response -> config loss or retirement -> identical chat replay; fresh writes 503; immutable policy/withdrawal page remains readable; forms closed; no automatic order POST' } }) + '\n')
} finally {
  await browser.close()
}
