import assert from 'node:assert/strict';
import {chromium,expect} from '@playwright/test';
import {writeFileSync} from 'node:fs';
const origin='https://localhost:45767';const b=await chromium.launch({args:['--no-sandbox']});
try{
const p=await b.newPage({viewport:{width:1440,height:1000},ignoreHTTPSErrors:false});

const out={tls:'trusted',policy:{},errors:[]};p.on('pageerror',e=>console.log('PAGEERROR',e.message));p.on('requestfailed',r=>console.log('FAILED',r.method(),r.url(),r.failure()));p.on('response',r=>{if(r.url().includes('/api/v1/'))console.log('RESPONSE',new URL(r.url()).pathname,r.status());});p.on('console',m=>{if(m.type()==='error')console.log('CONSOLE',m.text());});
for(const path of ['/privacy-policy','/api/v1/privacy-policy','/api/v1/consent-text']){const r=await p.request.get(origin+path);assert.equal(r.status(),200);out.policy[path]=r.status();}
const policy=await(await p.request.get(origin+'/api/v1/privacy-policy')).json();const consent=await(await p.request.get(origin+'/api/v1/consent-text')).json();
assert(policy.collection_enabled);assert.equal(policy.consent_version,consent.version);out.policy.collection_enabled=true;out.policy.consent_version_match=true;out.policy.version=consent.version;
await p.goto(origin+'/#catalog');await p.locator('.catalog-card').first().waitFor();
for(const [name,qty] of [['Стойка левая в сборе',1],['Первичный выезд для диагностики',1]]){const card=p.locator('.catalog-card').filter({has:p.getByRole('button',{name,exact:true})});await card.getByRole('button',{name:'Добавить в список'}).click();}
await p.getByRole('button',{name:'Оформить заявку',exact:true}).click();
const accept=p.getByRole('checkbox',{name:/Я прочитал/});const confirm=p.getByRole('checkbox',{name:/Подтверждаю показанные/});try {await expect(accept).toBeEnabled({timeout:30000});await expect(confirm).toBeEnabled({timeout:30000});} catch(e){console.log('FORM_DIAGNOSTIC',await p.locator('main:visible').innerText());console.log('DOCS',policy,consent);throw e;}assert(!await accept.isChecked());assert(!await confirm.isChecked());
await p.getByLabel(/Контактное имя/).fill('СИНТЕТИЧЕСКИЙ ПОКАЗ POLISH-001');await p.getByLabel('Email',{exact:true}).fill('polish-001@example.invalid');await p.getByLabel(/Организация/).fill('SYNTHETIC ONLY — учебная организация');
const city=p.getByLabel(/Город/);if(await city.count())await city.fill('Синтетический город');
await p.getByText('Прочитать политику и порядок обращения',{exact:true}).click();await expect(p.getByText(policy.text,{exact:true})).toBeVisible();
await accept.click();await confirm.focus();await p.keyboard.press('Space');await expect(accept).toBeChecked();await expect(confirm).toBeChecked();
await p.screenshot({path:'/evidence/stable-request-enabled.png',fullPage:true});
const responsePromise=p.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/orders'&&r.request().method()==='POST');await p.getByRole('button',{name:'Подтвердить и отправить заявку',exact:true}).click();
const response=await responsePromise;out.http_status=response.status();assert.equal(response.status(),201);const receipt=await response.json();out.receipt=receipt;await expect(p.getByRole('heading',{name:'Заявка №'+receipt.number})).toBeVisible();await p.screenshot({path:'/evidence/stable-order-created.png',fullPage:true});
// Preserve the exact logical identity and proof headers in memory only.
const req=response.request();const retry=await p.request.post(req.url(),{data:req.postData(),headers:req.headers()});out.retry_status=retry.status();const retried=await retry.json();assert.equal(retry.status(),200);assert.equal(retried.number,receipt.number);out.duplicate_protection=true;
out.checkboxes={mouse:true,keyboard:true,automatic_consent:false};writeFileSync('/evidence/stable-order.json',JSON.stringify(out,null,2));console.log(JSON.stringify(out));
}finally{await b.close();}
