// Local operator acceptance. Credentials are entered directly on this TTY,
// held in memory, and never placed in config, env, pipes, logs or screenshots.
import assert from 'node:assert/strict'
import { writeFileSync } from 'node:fs'
import { chromium, expect } from '@playwright/test'

async function hidden(prompt) {
  if (!process.stdin.isTTY) throw new Error('SECURE_TERMINAL_REQUIRED')
  process.stdout.write(prompt)
  process.stdin.setRawMode(true)
  process.stdin.resume()
  return new Promise((resolve, reject) => {
    let value = ''
    const done = () => {
      process.stdin.off('data', receive)
      process.stdin.setRawMode(false)
      process.stdin.pause()
      process.stdout.write('\n')
    }
    const receive = data => {
      for (const c of data.toString('utf8')) {
        if (c === '\u0003') { done(); reject(new Error('CANCELLED')); return }
        if (c === '\r' || c === '\n') { done(); resolve(value); return }
        if (c === '\u007f' || c === '\b') value = value.slice(0, -1)
        else if (c >= ' ') value += c
      }
    }
    process.stdin.on('data', receive)
  })
}
const origin = 'https://localhost:45767'
const number = 1
const browser = await chromium.launch({ args: ['--no-sandbox'] })
const results = { number, trusted_tls: true }
let stage = 'login'
try {
  for (const role of ['admin', 'manager']) {
    stage = role + '.login'
    const login = await hidden(`${role} login (manager: empty to skip): `)
    if (!login && role === 'manager') { results.manager = 'NOT_TESTED'; continue }
    const password = await hidden(`${role} password: `)
    const context = await browser.newContext({ ignoreHTTPSErrors: false, viewport: {width:1440,height:1000} })
    const page = await context.newPage()
    await page.goto(origin + '/admin/login')
    await page.getByLabel('Login', {exact:true}).fill(login)
    await page.getByLabel('Password', {exact:true}).fill(password)
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/admin/auth/login' && r.request().method() === 'POST')
    await page.getByRole('button', {name:'Log in',exact:true}).click()
    assert.equal((await response).status(), 303)
    await page.goto(origin + '/admin/orders')
    const row = page.locator('tr').filter({has:page.getByRole('cell',{name:String(number),exact:true})})
    await expect(row).toHaveCount(1)
    const link = row.getByRole('link').first()
    await link.click()
    await expect(page.getByRole('heading', {name:'Original item snapshots'})).toBeVisible()
    await expect(page.getByText('ЯИУШ.301421.011-02',{exact:true})).toBeVisible()
    await expect(page.getByText('DEMO-SRC-V01',{exact:true})).toBeVisible()
    await expect(page.getByText('SYNTHETIC-POLISH-001-v1',{exact:true})).toBeVisible()
    await page.screenshot({path:`/evidence/stable-order-${role}.png`,fullPage:true})
    stage = role + '.permissions'
    if(role==='manager') {
      assert.equal((await page.request.get(origin+'/admin/stats')).status(),200)
      assert.equal((await page.request.get(origin+'/admin/catalog-items')).status(),403)
    }
    // The browser owns the live session. The existing logout form clears it.
    const logout = page.getByRole('button',{name:/Log out|Logout|Выйти/i})
    if(await logout.count()) await logout.click()
    results[role] = 'PASS'
    writeFileSync('/evidence/stable-staff.json', JSON.stringify(results,null,2)+'\n')
    await context.close()
  }
  writeFileSync('/evidence/stable-staff.json', JSON.stringify(results,null,2)+'\n')
  console.log(JSON.stringify(results))
} catch {
  console.error('STAFF_ACCEPTANCE_FAILED stage=' + stage + '; no credentials or DOM were recorded')
  process.exitCode = 1
} finally { await browser.close() }
