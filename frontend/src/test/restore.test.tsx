import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import App from '../App'
import { AddControl } from '../components/AddControl'
import { DeliveryEstimate } from '../components/DeliveryEstimate'
import { useSelection, SELECTION_KEY } from '../state/selection'
import { fixtures } from '../api/fixtures'
import { CatalogPage } from '../pages/CatalogPage'
import { fixtureCatalog } from '../api/fixtureCatalog'
import { ChatController } from '../chat/controller'
import { ChatPage } from '../chat/ChatPage'
import { transport } from './chatFixtures'

it('uses the original quantity stepper without duplicate selection and retains the original revision', async () => {
  function Host() { const selection = useSelection(); return <AddControl item={fixtures.catalog_item} selection={selection} /> }
  render(<Host />)
  const user = userEvent.setup()
  await user.click(screen.getByRole('button',{name:'Добавить в заявку'}))
  expect(screen.queryByRole('button',{name:'Добавить в заявку'})).toBeNull()
  await user.click(screen.getByRole('button',{name:`Больше ${fixtures.catalog_item.sku}`}))
  await waitFor(()=>expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toEqual([{item_id:fixtures.catalog_item.id,qty:2,revision:fixtures.catalog_item.revision}]))
  await user.click(screen.getByRole('button',{name:`Меньше ${fixtures.catalog_item.sku}`}))
  await user.click(screen.getByRole('button',{name:`Убрать из заявки ${fixtures.catalog_item.sku}`}))
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toEqual([])
})
it('does not invent a delivery amount, handles an empty city and renders hostile city text safely', async () => {
  const view=render(<DeliveryEstimate />)
  await userEvent.click(screen.getByRole('button',{name:'Рассчитать'}))
  expect(screen.getByText('Укажите город получателя.')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Город получателя'),{target:{value:'<img src=x onerror=alert(1)>'}})
  await userEvent.click(screen.getByRole('button',{name:'Рассчитать'}))
  expect(view.container.querySelector('img')).toBeNull()
  expect(screen.getByText(/стоимость и срок доставки уточним/)).toBeTruthy()
})
it('keeps every original page URL on the real API source and exposes no fixture success on failure', async () => {
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:503}))
  for (const [path,title] of [['/zapchasti.html','Запчасти'],['/normy.html','Нормативная база'],['/zayavka.html','Заявка']] as const) {
    window.history.replaceState(null,'',path)
    const view=render(<App />)
    expect(screen.getByRole('heading',{name:title,level:1})).toBeTruthy()
    await waitFor(()=>expect(screen.getAllByRole('alert').length).toBeGreaterThan(0))
    expect(screen.queryByText(/Интерфейс на синтетических fixtures/)).toBeNull()
    view.unmount()
  }
  window.history.replaceState(null,'','/')
})
it('has the original header pages and puts the cart in a modal drawer, preserving its selection across routes', async () => {
  render(<CatalogPage source={fixtureCatalog} />)
  const user=userEvent.setup()
  const nav=within(screen.getByRole('navigation',{name:'Основное'}))
  expect(nav.getAllByRole('link').map(n=>n.textContent)).toEqual(['Помощник','Запчасти','Нормативная база','Техинформация','Заявка','Контакты'])
  await screen.findByRole('article',{name:`${fixtures.catalog_item.name}, ${fixtures.catalog_item.sku}`})
  await user.click(screen.getAllByRole('button',{name:'Добавить в заявку'})[0]!)
  expect(screen.queryByRole('dialog')).toBeNull()
  await user.click(screen.getByRole('button',{name:/Открыть корзину/}))
  expect(screen.getByRole('dialog',{name:'Состав заявки'})).toBeTruthy()
  await user.click(screen.getByRole('button',{name:'Закрыть корзину'}))
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toHaveLength(1)
  await act(async()=>{window.location.hash='#request'})
  expect(screen.getByRole('heading',{name:'Заявка',level:1})).toBeTruthy()
})
it('retains original quick choices and indicator panel while hiding implementation mode controls', async () => {
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:503}))
  const api=transport()
  const controller=new ChatController(api)
  function Host(){const selection=useSelection();return <ChatPage controller={controller} catalog={fixtureCatalog} selection={selection} onItem={()=>{}} onRequest={()=>{}} onBack={()=>{}} />}
  render(<Host />)
  await screen.findByRole('article',{name:'Актуальный ответ'})
  for(const name of ['Не включается','Ложные тревоги','Не считает проходы','Сигнал не прекращается','Нужна запчасть']) expect(screen.getByRole('button',{name})).toBeTruthy()
  expect(screen.getByRole('button',{name:'Индикатор «Готов»'})).toBeTruthy()
  expect(screen.getByRole('button',{name:'Индикатор «Тревога»'})).toBeTruthy()
  expect(screen.queryByRole('group',{name:'Режим помощника'})).toBeNull()
  expect(api.create).toHaveBeenCalledTimes(1)
  expect(api.send).not.toHaveBeenCalled()
})
it('does not show a local greeting or answer when session creation fails', async () => {
  const api=transport();api.create.mockRejectedValue(new Error('offline'))
  const controller=new ChatController(api)
  function Host(){const selection=useSelection();return <ChatPage controller={controller} catalog={fixtureCatalog} selection={selection} onItem={()=>{}} onRequest={()=>{}} onBack={()=>{}} />}
  render(<Host />)
  await screen.findByRole('alert')
  expect(screen.queryByText(/Здравствуйте!/)).toBeNull()
  expect(screen.queryByRole('article',{name:'Актуальный ответ'})).toBeNull()
  expect(screen.getByRole('button',{name:'Отправить'}).hasAttribute('disabled')).toBe(true)
})

it('opens an original-style item deep link and keeps real browser history between detail and list', async () => {
  window.history.replaceState(null, '', `/#part-${fixtures.catalog_item.id}`)
  render(<CatalogPage source={fixtureCatalog} />)
  await screen.findByText(fixtures.catalog_item.description)
  expect(screen.getAllByRole('main')).toHaveLength(1)
  await userEvent.click(screen.getByRole('button', {name:'Назад к списку'}))
  await screen.findByRole('region', {name:'Результаты каталога'})
  expect(window.location.hash).toBe('#catalog')
  act(()=>window.history.back())
  await screen.findByText(fixtures.catalog_item.description)
  expect(window.location.hash).toBe(`#part-${fixtures.catalog_item.id}`)
  expect(screen.getAllByRole('main')).toHaveLength(1)
})

it('routes free text through the existing knowledge mode without exposing three implementation modes', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok:false,status:503}))
  const api=transport()
  const {result,reply}=await import('./chatFixtures')
  api.send.mockResolvedValueOnce(result(1,reply('knowledge'))).mockResolvedValueOnce(result(2,reply('knowledge',{text:'Published search has no match'})))
  const controller=new ChatController(api)
  function Host(){const selection=useSelection();return <ChatPage controller={controller} catalog={fixtureCatalog} selection={selection} onItem={()=>{}} onRequest={()=>{}} onBack={()=>{}} />}
  render(<Host />)
  await screen.findByRole('article',{name:'Актуальный ответ'})
  fireEvent.change(screen.getByLabelText('Сообщение'),{target:{value:'SYNTHETIC source query'}})
  await userEvent.click(screen.getByRole('button',{name:'Отправить'}))
  await screen.findByText('Published search has no match')
  expect(api.send.mock.calls.map(([wire])=>JSON.parse(wire).input)).toEqual([{type:'select_mode',mode:'knowledge'},{type:'text',value:'SYNTHETIC source query'}])
  expect(api.create).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('group',{name:'Режим помощника'})).toBeNull()
  expect((screen.getByLabelText('Сообщение') as HTMLInputElement).value).toBe('')
})

it('keeps a free-text query on an uncertain mode change and retries exactly that original command', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok:false,status:503}))
  const api=transport()
  const {result,reply}=await import('./chatFixtures')
  const {ChatError}=await import('../api/httpChat')
  api.send.mockRejectedValueOnce(new ChatError(503,'SERVICE_UNAVAILABLE')).mockResolvedValueOnce(result(1,reply('knowledge'))).mockResolvedValueOnce(result(2,reply('knowledge')))
  const controller=new ChatController(api)
  function Host(){const selection=useSelection();return <ChatPage controller={controller} catalog={fixtureCatalog} selection={selection} onItem={()=>{}} onRequest={()=>{}} onBack={()=>{}} />}
  render(<Host />)
  await screen.findByRole('article',{name:'Актуальный ответ'})
  fireEvent.change(screen.getByLabelText('Сообщение'),{target:{value:'SYNTHETIC retained query'}})
  await userEvent.click(screen.getByRole('button',{name:'Отправить'}))
  await screen.findByText(/Исход запроса неизвестен/)
  expect(api.send).toHaveBeenCalledTimes(1)
  expect((screen.getByLabelText('Сообщение') as HTMLInputElement).value).toBe('SYNTHETIC retained query')
  await userEvent.click(screen.getByRole('button',{name:'Повторить тот же запрос'}))
  await waitFor(()=>expect(screen.queryByText(/Исход запроса неизвестен/)).toBeNull())
  expect(api.send.mock.calls[1]![0]).toBe(api.send.mock.calls[0]![0])
  expect(api.send).toHaveBeenCalledTimes(2)
  expect((screen.getByLabelText('Сообщение') as HTMLInputElement).value).toBe('SYNTHETIC retained query')
  await userEvent.click(screen.getByRole('button',{name:'Отправить'}))
  await waitFor(()=>expect(api.send).toHaveBeenCalledTimes(3))
  expect(JSON.parse(api.send.mock.calls[2]![0]).input).toEqual({type:'text',value:'SYNTHETIC retained query'})
})
