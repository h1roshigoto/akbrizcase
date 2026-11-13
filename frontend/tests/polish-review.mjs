import assert from 'node:assert/strict';
import {chromium,expect} from '@playwright/test';
import {writeFileSync,readFileSync} from 'node:fs';
const config=JSON.parse(readFileSync(0,'utf8'));const origin=config.origin;const b=await chromium.launch({args:['--no-sandbox']});const out={tls:'trusted',http:[],errors:[]};
try{

for (const [size,viewport] of [['desktop',{width:1440,height:1000}],['mobile',{width:390,height:844}]]) {
 for (const theme of ['light','dark']) {
  const page=await b.newPage({ignoreHTTPSErrors:false,viewport,reducedMotion:'reduce'});
  await page.addInitScript(theme=>localStorage.setItem('mtdka-theme',theme),theme);
  await page.goto(origin+'/#catalog');
  await expect(page.locator('.catalog-card')).toHaveCount(16);
  await expect(page.locator('html')).toHaveAttribute('data-theme',theme);
  const stand=page.locator('.catalog-card').filter({hasText:'ЯИУШ.301421.011-02'});
  await expect(stand.getByRole('img',{name:'На заказ',exact:true})).toBeVisible();
  await stand.screenshot({path:`/evidence/review-stand-${size}-${theme}.png`});
  await stand.getByRole('button',{name:'Стойка левая в сборе',exact:true}).click();
  await expect(page.locator('.item-detail').getByRole('img',{name:'На заказ'})).toBeVisible();
  await page.screenshot({path:`/evidence/review-stand-detail-${size}-${theme}.png`,fullPage:true});
  await page.getByRole('button',{name:/Вернуться в каталог/}).click();
  await page.getByRole('button',{name:'Услуги',exact:true}).click();
  await expect(page.locator('.catalog-card')).toHaveCount(5);
  await expect(page.locator('.catalog-card').getByRole('img',{name:'Сервисная услуга',exact:true})).toHaveCount(5);
  assert.equal(await page.getByText('Изображение отсутствует',{exact:true}).count(),0);
  await page.screenshot({path:`/evidence/review-services-${size}-${theme}.png`,fullPage:true});
  for (const card of await page.locator('.catalog-card').all()) {
   const name=await card.locator('.title-button').innerText();
   await card.locator('.title-button').click();
   await expect(page.locator('.item-detail').getByRole('img',{name:'Сервисная услуга'})).toBeVisible();
   await page.screenshot({path:`/evidence/review-service-${name.split(' ')[0]}-${size}-${theme}.png`,fullPage:true});
   await page.getByRole('button',{name:/Вернуться в каталог/}).click();
  }
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.close();
 }
}
out.catalog_desktop=true;out.catalog_mobile=true;out.themes=['light','dark'];
const p=await b.newPage({ignoreHTTPSErrors:false,viewport:{width:1440,height:1000}});p.on('pageerror',()=>out.errors.push('pageerror'));p.on('response',r=>{if(r.url().includes('/api/v1/chat'))out.http.push({path:new URL(r.url()).pathname,status:r.status()});});
await p.goto(origin+'/#chat');const current=p.getByRole('article',{name:'Актуальный ответ'});await expect(current).toBeVisible({timeout:30000});
const select=p.getByLabel('Модель для диагностики');await expect(select).toBeEnabled();const models=await(await p.request.get(origin+'/api/v1/equipment-models')).json();out.models=models.total;
const model=models.items.find(m=>m.code==='MTD-KA-01');await select.selectOption(model.id);await expect(p.getByRole('button',{name:'SYNTHETIC ONLY — видна ли надпись DEMO?'})).toBeVisible();await p.screenshot({path:'/evidence/review-assistant-model.png',fullPage:true});
await p.getByRole('button',{name:'SYNTHETIC ONLY — видна ли надпись DEMO?'}).click();await expect(current.getByRole('button',{name:'Да, вижу DEMO на учебном экране'})).toBeEnabled();await p.screenshot({path:'/evidence/review-diagnosis-question.png',fullPage:true});
await p.screenshot({path:'/evidence/review-assistant-directions.png',fullPage:true});await p.getByRole('button',{name:'Подбор по шагам',exact:true}).click();await current.getByRole('button',{name:'Да, вижу DEMO на учебном экране'}).click();await expect(current.getByText(/Учебная проверка завершена/)).toBeVisible();await p.screenshot({path:'/evidence/review-diagnosis-result.png',fullPage:true});
const feedback=current.getByRole('button',{name:'Помогло',exact:true});console.log('CURRENT_RESULT',await current.innerText());await feedback.click();await expect(current.getByText('Диагностика завершена',{exact:true})).toBeVisible();out.diagnostic_complete_path=true;
await p.reload();await expect(current.getByText('Диагностика завершена',{exact:true})).toBeVisible({timeout:30000});out.restored=true;
await p.getByRole('button',{name:'Начать другой сценарий',exact:true}).click().catch(()=>{});
// Reload remounts the menu; verify unsupported MTD-KA-18 offers no invented tree.
await select.selectOption(models.items.find(m=>m.code==='MTD-KA-18').id);await expect(p.getByText(/Для этой модели нет доступных опубликованных сценариев/)).toBeVisible();out.unsupported_model=true;
await p.getByRole('button',{name:/^Найти документацию/}).click();await p.getByLabel('Запрос по документации').fill('учебная');await p.getByRole('button',{name:'Отправить запрос',exact:true}).click();await expect(current.getByText(/Цитаты из опубликованных материалов/)).toBeVisible();out.knowledge=true;await p.screenshot({path:'/evidence/review-knowledge.png',fullPage:true});
const source=current.getByRole('link').first();const file=await p.request.get(new URL(await source.getAttribute('href'),origin).href);out.source_file_status=file.status();
await p.getByRole('button',{name:/^Подобрать запчасти/}).click();await p.getByLabel('Поиск товаров и услуг').fill('ЯИУШ.301421.011-02');await p.getByRole('button',{name:'Отправить запрос',exact:true}).click();await expect(current.getByText('ЯИУШ.301421.011-02',{exact:true})).toBeVisible();await current.getByRole('button',{name:'Открыть карточку: Стойка левая в сборе',exact:true}).click();await expect(p.locator('.item-detail h2')).toHaveText('Стойка левая в сборе');out.commerce=true;
await p.goto(origin+'/#chat');await expect(current).toBeVisible();await p.getByRole('button',{name:/^Диагностика оборудования/}).click();await expect(select).toBeEnabled();await select.selectOption(model.id);await p.getByRole('button',{name:'SYNTHETIC ONLY — видна ли надпись DEMO?'}).click();await current.getByRole('button',{name:'Нет или не уверен — обратиться к специалисту'}).click();await current.getByRole('button',{name:'Связаться со специалистом',exact:true}).click();await expect(p.getByRole('heading',{name:'Обращение к специалисту',exact:true})).toBeVisible();out.support_handoff='form shown, no auto submission';await p.screenshot({path:'/evidence/review-support-handoff.png',fullPage:true});
assert.deepEqual(out.errors,[]);out.chat_sessions_created=out.http.filter(x=>x.path==='/api/v1/chat/sessions'&&x.status===201).length;assert.equal(out.chat_sessions_created,1);out.shared_session=true;
writeFileSync('/evidence/review-acceptance.json',JSON.stringify(out,null,2));console.log(JSON.stringify(out));
}finally{await b.close();}
