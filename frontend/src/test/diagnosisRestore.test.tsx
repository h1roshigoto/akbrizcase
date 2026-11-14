import { useState } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { ChatPage } from '../chat/ChatPage'
import { ChatController } from '../chat/controller'
import { ChatError } from '../api/httpChat'
import { fixtureCatalog } from '../api/fixtureCatalog'
import { fixtures } from '../api/fixtures'
import { useSelection } from '../state/selection'
import { reply, result, transport } from './chatFixtures'
import type { ChatMessageResult, ChatReply } from '../api/types'

const question = () => reply('diagnose', { text: 'SYNTHETIC approved question', resolution: 'question', buttons: [{ id: 'observe', label: 'SYNTHETIC observation', input: { type: 'button', option_id: 'observed' } }] })
function mount({ lose = '' } = {}) {
  const api = transport()
  let version = 0, transitions = 0, lost = false
  let current: ChatReply = reply()
  const saved = new Map<string, ChatMessageResult>()
  api.send.mockImplementation(async wire => {
    const { request_id, input } = JSON.parse(wire)
    if (saved.has(request_id)) return saved.get(request_id)!
    if (input.type === 'select_mode') {
      if (current.mode !== input.mode) current = reply(input.mode, { buttons: input.mode === 'diagnose' ? [{ id: 'scenario', label: 'Unrelated server scenario', input: { type: 'start_diagnosis', model_id: fixtures.models.items[0]!.id, tree_key: 'unrelated' } }] : [] })
    } else if (input.type === 'start_diagnosis') current = question()
    else if (input.type === 'button') { transitions++; current = reply('diagnose', { text: 'SYNTHETIC approved result', resolution: 'results' }) }
    const response = result(++version, current)
    saved.set(request_id, response)
    if (!lost && (lose === 'reset' && input.mode === 'knowledge' || lose === 'transition' && input.type === 'button')) { lost = true; throw new ChatError(503, 'UNCERTAIN') }
    return response
  })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => url.includes('equipment-models') ? fixtures.models : { items: [{ key: 'unrelated', title: 'Не включается', model_id: fixtures.models.items[0]!.id }] } })))
  const controller = new ChatController(api)
  function Host() {
    const selection = useSelection()
    const [presentation, setPresentation] = useState<'chat' | 'steps'>('chat')
    return <ChatPage controller={controller} catalog={fixtureCatalog} selection={selection} onItem={() => {}} onRequest={() => {}} onBack={() => {}} presentation={presentation} onPresentation={setPresentation} />
  }
  render(<Host />)
  return { api, controller, transitions: () => transitions, user: userEvent.setup() }
}
const active = () => within(screen.getByRole('article', { name: 'Актуальный ответ' }))
// The quick choice and deliberately identical scenario title need scoped clicks.
async function startGeneral(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: fixtures.models.items[0]!.name }))
  await user.click(await within(screen.getByLabelText('Опубликованные сценарии')).findByRole('button', { name: 'Не включается' }))
  await active().findByText('SYNTHETIC approved question')
}
it.each(['Не включается', 'Ложные тревоги', 'Не считает проходы', 'Сигнал не прекращается'])('does not infer a scenario mapping for %s or keep old actions under it', async symptom => {
  const { api, user } = mount()
  await startGeneral(user)
  await user.click(screen.getByRole('button', { name: symptom }))
  await screen.findByText(/нет подтверждённого соответствия опубликованному сценарию/)
  expect(screen.getByRole('heading', { name: `Неисправность: ${symptom}` })).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Открыть документацию' }).getAttribute('href')).toBe('#docs')
  expect(active().queryByRole('button', { name: 'SYNTHETIC observation' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Unrelated server scenario' })).toBeNull()
  expect(screen.queryByLabelText('Опубликованные сценарии')).toBeNull()
  expect(api.send.mock.calls.map(([wire]) => JSON.parse(wire).input).slice(-2)).toEqual([{ type: 'select_mode', mode: 'knowledge' }, { type: 'select_mode', mode: 'diagnose' }])
  expect(api.send.mock.calls.filter(([wire]) => JSON.parse(wire).input.type === 'start_diagnosis')).toHaveLength(1)
})
it('really resets progressed diagnosis, shares chat/wizard state and replays a lost transition once', async () => {
  const { api, user, transitions } = mount({ lose: 'transition' })
  await startGeneral(user)
  await user.click(active().getByRole('button', { name: 'SYNTHETIC observation' }))
  await screen.findByText(/Исход запроса неизвестен/)
  const wire = api.send.mock.calls.at(-1)![0]
  await user.click(screen.getByRole('button', { name: 'Повторить тот же запрос' }))
  await screen.findByText('SYNTHETIC approved result')
  expect(api.send.mock.calls.at(-1)![0]).toBe(wire)
  expect(transitions()).toBe(1)
  await user.click(screen.getByRole('tab', { name: 'Подбор по шагам' }))
  await user.click(screen.getByRole('button', { name: 'Начать заново' }))
  await screen.findByRole('heading', { name: 'Какая модификация у изделия?' })
  expect(active().queryByText('SYNTHETIC approved result')).toBeNull()
  await user.click(screen.getByRole('tab', { name: 'Чат с помощником' }))
  await startGeneral(user)
  expect(active().getByRole('button', { name: 'SYNTHETIC observation' })).toBeTruthy()
  expect(api.create).toHaveBeenCalledTimes(1)
})
it('keeps an uncertain reset command for exact retry without relabelling the old diagnostic actions', async () => {
  const { api, user } = mount({ lose: 'reset' })
  await startGeneral(user)
  await user.click(screen.getByRole('button', { name: 'Ложные тревоги' }))
  await screen.findByText(/Исход запроса неизвестен/)
  expect(screen.queryByRole('heading', { name: 'Неисправность: Ложные тревоги' })).toBeNull()
  expect(active().getByRole('button', { name: 'SYNTHETIC observation' }).hasAttribute('disabled')).toBe(true)
  const wire = api.send.mock.calls.at(-1)![0]
  await user.click(screen.getByRole('button', { name: 'Повторить тот же запрос' }))
  await waitFor(() => expect(screen.queryByText(/Исход запроса неизвестен/)).toBeNull())
  expect(api.send.mock.calls.at(-1)![0]).toBe(wire)
  expect(active().queryByRole('button', { name: 'SYNTHETIC observation' })).toBeNull()
  await user.click(screen.getByRole('button', { name: 'Ложные тревоги' }))
  await screen.findByRole('heading', { name: 'Неисправность: Ложные тревоги' })
})
