import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from 'react'
import type { CatalogSource } from '../api/catalog'
import type { ChatReply, UUID } from '../api/types'
import { MAX_REQUEST_ITEMS, type Selection } from '../state/selection'
import { ChatController } from './controller'
import { DiagnosisMenu } from './DiagnosisMenu'
import { ReplyView } from './ReplyView'
import detector from '../design/assets/detector.png'

export function ChatPage({
  controller,
  catalog,
  selection,
  onItem,
  onRequest,
  onSupport,
  onBack,
  presentation = 'chat',
  onPresentation,
}: {
  controller: ChatController
  catalog: CatalogSource
  selection: Selection
  onItem: (id: UUID) => void
  onRequest: () => void
  onSupport?: (
    draft: string | null,
    owner: { session_id: UUID; session_token: string },
  ) => void
  onBack: () => void
  presentation?: 'chat' | 'steps'
  onPresentation?: (view: 'chat' | 'steps') => void
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot)
  const [opened, setOpened] = useState(false)
  const [text, setText] = useState('')
  const [symptom, setSymptom] = useState<string | null>(null)
  const [pickRevision, setPickRevision] = useState(0)
  const [actionBusy, setActionBusy] = useState(false)
  const actionLock = useRef(false)
  const alive = useRef<AbortController | null>(null)
  const [actionNotice, setActionNotice] = useState('')
  useEffect(() => {
    const lifetime = new AbortController()
    alive.current = lifetime
    void controller.open().then(() => {
      if (!lifetime.signal.aborted) setOpened(true)
    })
    return () => {
      lifetime.abort()
    }
  }, [controller])
  const disabled =
    state.busy || !state.ready || state.pending || actionBusy || !opened
  const reply = opened ? state.reply : null
  const visibleNotice = /^Режим (переключён|подтверждён) сервером\./.test(state.notice) ? '' : state.notice
  useEffect(() => {
    if (!opened || presentation !== 'steps') return
    const current = controller.snapshot()
    if (current.ready && !current.busy && !current.pending && current.reply?.mode !== 'diagnose')
      void controller.send({ type: 'select_mode', mode: 'diagnose' })
  }, [controller, opened, presentation])
  const act = async (action: ChatReply['actions'][number]) => {
    const current = controller.snapshot()
    if (
      actionLock.current ||
      disabled ||
      current.reply !== reply ||
      current.busy ||
      current.pending ||
      !reply?.actions.includes(action)
    )
      return
    if (
      action.type === 'contact_specialist' ||
      (action.type === 'open_request_form' && action.payload.kind === 'support')
    ) {
      const owner = controller.supportContext(action)
      if (owner && onSupport)
        onSupport(
          'comment_draft' in action.payload
            ? (action.payload.comment_draft ?? null)
            : null,
          owner,
        )
      return
    }
    const signal = alive.current!.signal
    actionLock.current = true
    setActionBusy(true)
    setActionNotice('')
    try {
      if (action.type === 'open_item') {
        await catalog.getItem(action.payload.item_id, null, signal)
        if (!signal.aborted) onItem(action.payload.item_id)
      } else if (
        action.type === 'open_request_form' &&
        action.payload.kind === 'commercial'
      ) {
        const suggestions = action.payload.items
        // Reload every public position. Server prices/revisions never come from chat/storage.
        const cards = await Promise.all(
          suggestions.map((s) => catalog.getItem(s.item_id, null, signal)),
        )
        if (!signal.aborted) {
          const result = selection.addSuggestions(
            cards.map((card, i) => ({ card, qty: suggestions[i]!.qty })),
          )
          if (!result) {
            setActionNotice(
              `Не удалось подготовить заявку. В одной заявке допускается не более ${MAX_REQUEST_ITEMS} уникальных позиций. Проверьте выбранное. Список не изменён, заявка не отправлена.`,
            )
            return
          }
          onRequest()
        }
      }
    } catch {
      if (!signal.aborted)
        setActionNotice(
          'Не удалось подготовить действие. Проверьте доступность позиций и лимит выбранного, затем повторите. Заявка не отправлена.',
        )
    } finally {
      actionLock.current = false
      if (!signal.aborted) setActionBusy(false)
    }
  }
  const openItem = (id: UUID) => {
    const action = reply?.actions.find(
      (a) => a.type === 'open_item' && a.payload.item_id === id,
    )
    if (action) void act(action)
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (disabled || !text.trim() || text.length > 1000) return
    const submitted = text
    void (async () => {
      const current = controller.snapshot()
      const inDiagnosis = current.reply?.mode === 'diagnose' && current.reply.buttons.some(b => b.input.type === 'button')
      if (current.reply?.mode === 'diagnose' && !inDiagnosis) {
        await controller.send({ type: 'select_mode', mode: 'knowledge' })
        const next = controller.snapshot()
        if (!next.ready || next.error || next.pending || next.reply?.mode !== 'knowledge') return
      }
      await controller.send({ type: 'text', value: submitted })
      const result = controller.snapshot()
      if (!result.error && !result.pending && !result.notice)
        setText(value => value === submitted ? '' : value)
    })()
  }
  const askIndicator = async (name: string) => {
    if (disabled) return
    await controller.send({ type: 'select_mode', mode: 'knowledge' })
    const next = controller.snapshot()
    if (next.ready && !next.error && !next.pending && next.reply?.mode === 'knowledge')
      await controller.send({ type: 'text', value: `Индикатор: ${name}` })
  }
  const replies = <>
    {state.next && <button className="chip" disabled={disabled} onClick={() => void controller.more()}>Загрузить предыдущие ответы</button>}
    {state.messages.filter(m => m.state_version !== state.version).map(m => <ReplyView key={m.message_id} reply={m.reply} />)}
    {reply && <ReplyView reply={reply} current disabled={disabled} onInput={input => { setActionNotice(''); void controller.send(input) }} onAction={action => void act(action)} onItem={openItem} />}
  </>
  const diagnosis = reply?.mode === 'diagnose' && <DiagnosisMenu key={pickRevision} disabled={disabled} symptom={symptom} onStart={input => { setActionNotice(''); void controller.send(input) }} />
  const recovery = <>
    {state.storageFailed && <p className="inline-error" role="alert">Хранилище сессии недоступно или запись не удалась. Восстановление после обновления страницы и сохранение запроса для повтора не гарантируются.</p>}
    {visibleNotice && <p role="status">{visibleNotice}</p>}
    {state.error && <p className="inline-error" role="alert">{state.error}</p>}
    {state.busy && <p role="status">Загружаем ответ…</p>}
    {actionNotice && <p role="status">{actionNotice}</p>}
    <div className="assistant-recovery">
      {state.terminal ? <button className="chip" disabled={state.busy} onClick={() => void controller.start()}>Создать новую сессию</button> : <>
        {(state.error || visibleNotice || state.pending) && <button className="chip" disabled={state.busy || actionBusy} onClick={() => void controller.refresh()}>Перечитать сессию</button>}
        {state.pending && <button className="chip" disabled={state.busy || actionBusy} onClick={() => void controller.retry()}>Повторить тот же запрос</button>}
      </>}
    </div>
  </>
  return <main><div className="wrap restore-chat">
    <div className="wrap pg"><h1>Помощник</h1><p className="lead">Опишите, что случилось, или пройдите подбор по шагам. Ответы только по опубликованным документам.</p></div>
    <div className="chatgrid"><div className="chatcol">
      <div className="tabs" role="tablist" aria-label="Способ поиска неисправности">{(['chat','steps'] as const).map((tab, i) => <button key={tab} role="tab" type="button" id={i ? 't-wizard' : 't-chat'} aria-controls={i ? 'p-wizard' : 'p-chat'} aria-selected={presentation === tab} tabIndex={presentation === tab ? 0 : -1} onClick={() => onPresentation?.(tab)} onKeyDown={e => { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {e.preventDefault(); onPresentation?.(i ? 'chat' : 'steps'); document.getElementById(i ? 't-chat' : 't-wizard')?.focus()} }}>{i ? 'Подбор по шагам' : 'Чат с помощником'}</button>)}</div>
      <section className="panel chat" id="p-chat" role="tabpanel" aria-labelledby="t-chat" hidden={presentation !== 'chat'}>
        <div className="top"><div className="ava"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2v3M5 9h14v10H5zM2 12h3m14 0h3M8 13h2m4 0h2" /></svg></div><div><b>Помощник по МТД-КА</b><small>Отвечает по документам завода</small></div></div>
        <div className="msgs" id="msgs" role="log" aria-live="polite">
          <p className="chat-privacy">Не указывайте в чате ФИО, телефон, email и другие персональные данные. Для обращения используйте предназначенную для этого форму.</p>
          {reply && <><div className="m bot">Здравствуйте! Я помогу разобраться с неисправностью металлодетектора МТД-КА и заказать запчасть. Что случилось?</div>
            <div className="chips">{['Не включается','Ложные тревоги','Не считает проходы','Сигнал не прекращается'].map(label => <button className="chip" disabled={disabled} key={label} onClick={async () => { setSymptom(label); setPickRevision(n => n+1); if (reply.mode !== 'diagnose') await controller.send({type:'select_mode',mode:'diagnose'}) }}>{label}</button>)}<button className="chip" disabled={disabled} onClick={() => void controller.send({type:'select_mode',mode:'commerce'})}>Нужна запчасть</button><button className="chip" disabled={disabled} onClick={() => onPresentation?.('steps')}>Подбор по шагам</button></div>
            {symptom && <div className="m me">{symptom}</div>}
            {presentation === 'chat' && <>{diagnosis}{replies}{reply.mode === 'commerce' && <div className="chips"><button className="chip" disabled={disabled} onClick={onBack}>Перейти в каталог</button></div>}</>}</>}
          {presentation === 'chat' && recovery}
        </div>
        <form className="inp" id="chatform" onSubmit={submit} autoComplete="off"><label className="sr" htmlFor="q">Сообщение</label><input className="in" id="q" value={text} onChange={e => setText(e.target.value)} disabled={disabled} placeholder="Опишите, что случилось…" maxLength={1000} /><button className="send" disabled={disabled || !text.trim()} type="submit" aria-label="Отправить"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 3 19 9-19 9 3-8 9-1-9-1z" /></svg></button></form>
      </section>
      <section className="panel wz" id="p-wizard" role="tabpanel" aria-labelledby="t-wizard" hidden={presentation !== 'steps'}><div id="wizard">
        {presentation === 'steps' && <>{recovery}{reply && <>{diagnosis}{replies}<div className="wz-end"><button className="lnk" onClick={() => onPresentation?.('chat')}>Обсудить в чате</button><button className="lnk" disabled={disabled} onClick={() => {setSymptom(null); void controller.send({type:'select_mode',mode:'diagnose'})}}>Начать заново</button></div></>}</>}
      </div></section>
    </div><aside className="side"><section className="panel"><h3>Что показывает панель?</h3>
      <div className="panelimg"><div className="arch" aria-hidden="true"><div className="top" /><div className="post l"><i className="led" /></div><div className="post r"><i className="led" /></div></div><img data-slot="" src={detector} alt="" loading="lazy" /><button className="hot g" type="button" disabled={disabled} aria-label="Индикатор «Готов»" onClick={() => void askIndicator('Готов')} /><button className="hot a" type="button" disabled={disabled} aria-label="Индикатор «Тревога»" onClick={() => void askIndicator('Тревога')} /></div>
      <p className="hint">Нажмите на индикатор на изображении, и помощник уточнит, что он означает.</p><div className="legend"><span><i className="g" />Готов</span><span><i className="a" />Тревога</span></div>
    </section><section className="panel"><h3>Документы по теме</h3><a className="doc" href="#docs"><b>Нормативные акты и документы завода</b><small>Опубликованные источники</small></a></section></aside></div>
  </div></main>
}
