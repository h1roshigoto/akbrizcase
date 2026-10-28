import {
  ChatError,
  isChatInput,
  type ChatInput,
  type ChatMode,
  type ChatSource,
} from '../api/httpChat'
import type {
  ChatExchangeRead,
  ChatReply,
  ChatMessageResult,
  UUID,
} from '../api/types'
import { CHAT_KEY, parseOwner, type Owner } from './storage'

type Exchange = Pick<ChatExchangeRead, 'message_id' | 'state_version' | 'reply'>
export type ChatState = {
  busy: boolean
  ready: boolean
  terminal: boolean
  pending: boolean
  storageFailed: boolean
  error: string
  notice: string
  version: number
  reply: ChatReply | null
  messages: Exchange[]
  next: UUID | null
}
const initial = (): ChatState => ({
  busy: false,
  ready: false,
  terminal: false,
  pending: false,
  storageFailed: false,
  error: '',
  notice: '',
  version: 0,
  reply: null,
  messages: [],
  next: null,
})

/** One serialized owner/controller above view mounts. Never store replies or history. */
export class ChatController {
  private state = initial()
  private owner: Owner | null = null
  private listeners = new Set<() => void>()
  private opening: Promise<void> | null = null
  private initialized = false
  private refreshWhenIdle = false
  private pages = 1
  constructor(private source: ChatSource) {}
  snapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }
  private set(patch: Partial<ChatState>) {
    this.state = { ...this.state, ...patch }
    this.listeners.forEach((l) => l())
  }
  private persist() {
    try {
      if (this.owner)
        sessionStorage.setItem(CHAT_KEY, JSON.stringify(this.owner))
      else sessionStorage.removeItem(CHAT_KEY)
    } catch {
      this.set({ storageFailed: true })
    }
  }
  private end(message: string) {
    this.owner = null
    this.persist()
    this.set({
      ...initial(),
      terminal: true,
      error: message,
      storageFailed: this.state.storageFailed,
    })
  }
  private unavailable(error: unknown): boolean {
    if (
      error instanceof ChatError &&
      (error.status === 404 || error.status === 410)
    ) {
      this.end(
        error.status === 410
          ? 'Срок сессии истёк. Создайте новую сессию; предыдущая команда не будет отправлена.'
          : 'Сессия недоступна. Можно явно начать новую.',
      )
      return true
    }
    return false
  }
  open(): Promise<void> {
    // StrictMode's second setup shares the exact in-flight open, including create.
    if (this.opening) return this.opening
    this.opening = this.openOnce().finally(() => {
      this.opening = null
    })
    return this.opening
  }
  private finish() {
    this.set({ busy: false })
    if (this.refreshWhenIdle) {
      this.refreshWhenIdle = false
      void this.refresh()
    }
  }
  private async openOnce() {
    if (this.state.busy) {
      this.refreshWhenIdle = true
      return
    }
    if (!this.initialized) {
      this.initialized = true
      try {
        this.owner = parseOwner(sessionStorage.getItem(CHAT_KEY))
      } catch {
        this.set({ storageFailed: true })
        // Invalid saved input never authorizes a command or an action.
        this.owner = null
        this.persist()
      }
      if (!this.owner) {
        await this.start()
        return
      }
    }
    if (this.owner) await this.refresh()
  }
  async start(mode: ChatMode = 'diagnose') {
    if (this.state.busy || this.owner) return
    this.initialized = true
    this.set({
      busy: true,
      error: '',
      terminal: false,
      ready: false,
      reply: null,
      messages: [],
    })
    try {
      const created = await this.source.create(mode)
      this.owner = {
        session_id: created.session_id,
        session_token: created.session_token,
        expires_at: created.expires_at,
        pending: null,
      }
      this.persist()
      this.set({
        ready: true,
        version: 0,
        reply: created.reply,
        notice:
          'Сессия действует 24 часа с момента создания. Активность не продлевает срок.',
      })
    } catch {
      this.set({
        terminal: true,
        error: 'Не удалось создать сессию. Повторите создание явно.',
      })
    } finally {
      this.finish()
    }
  }
  private async read(before?: UUID) {
    if (!this.owner) return
    const owner = this.owner
    const page = await this.source.read(
      owner.session_id,
      owner.session_token,
      before,
    )
    if (this.owner !== owner) return
    if (page.state_version < this.state.version)
      throw new ChatError(503, 'STALE_READ')
    if (before && page.state_version !== this.state.version) {
      // Concurrent writer: discard every cached page; fetch fresh first page.
      this.set({ reply: null, messages: [], next: null, ready: false })
      await this.read()
      this.set({
        notice:
          'Состояние изменилось. История загружена заново; выберите актуальное действие.',
      })
      return
    }
    const messages = before
      ? [...this.state.messages, ...page.messages]
      : page.messages
    const unique = [
      ...new Map(messages.map((m) => [m.message_id, m])).values(),
    ].sort(
      (a, b) =>
        a.state_version - b.state_version ||
        a.message_id.localeCompare(b.message_id),
    )
    this.owner = { ...owner, expires_at: page.expires_at }
    this.persist()
    this.set({
      reply: page.reply,
      version: page.state_version,
      messages: unique,
      next: page.next_before,
      ready: true,
      pending: !!this.owner.pending,
    })
  }
  async refresh() {
    if (this.state.busy || !this.owner) return
    this.pages = 1
    this.set({
      busy: true,
      ready: false,
      reply: null,
      messages: [],
      next: null,
      error: '',
    })
    try {
      await this.read()
    } catch (error) {
      if (!this.unavailable(error))
        this.set({
          error: 'Не удалось перечитать сессию. Повторите загрузку.',
          ready: false,
        })
    } finally {
      this.finish()
    }
  }
  async more() {
    if (
      this.state.busy ||
      !this.state.ready ||
      !this.state.next ||
      this.state.pending
    )
      return
    const target = this.pages + 1
    this.set({
      busy: true,
      ready: false,
      error: '',
      reply: null,
      messages: [],
      next: null,
    })
    try {
      // Every history request refreshes previously displayed pages as well. A
      // cached quote is never retained merely because an older page was loaded.
      await this.read()
      this.pages = 1
      while (this.pages < target && this.state.next) {
        await this.read(this.state.next)
        this.pages++
      }
    } catch (error) {
      // Do not leave previously authorized technical replies after a failed read.
      if (!this.unavailable(error))
        this.set({
          ready: false,
          reply: null,
          messages: [],
          next: null,
          error: 'Не удалось загрузить историю. Перечитайте сессию.',
        })
    } finally {
      this.finish()
    }
  }
  async send(input: ChatInput) {
    if (
      this.state.busy ||
      !this.state.ready ||
      !this.owner ||
      this.owner.pending ||
      !isChatInput(input)
    )
      return
    this.set({ busy: true, error: '', notice: '' })
    try {
      this.owner.pending = JSON.stringify({
        session_id: this.owner.session_id,
        request_id: crypto.randomUUID(),
        expected_version: this.state.version,
        input,
      })
    } catch {
      this.set({
        busy: false,
        error: 'Не удалось подготовить запрос. Повторите действие.',
      })
      return
    }
    this.persist() // Exact wire string is persisted BEFORE POST; never reserialized on retry.
    this.set({ pending: true })
    await this.post()
  }
  async retry() {
    if (this.state.busy || !this.owner?.pending) return
    this.set({ busy: true, error: '' })
    await this.post()
  }
  private accept(result: ChatMessageResult) {
    // Replay may carry a fresh safety projection at the same version/ID.
    const messages = new Map(this.state.messages.map((m) => [m.message_id, m]))
    messages.set(result.message_id, result)
    this.set({
      reply: result.reply,
      version: result.state_version,
      ready: true,
      messages: [...messages.values()].sort(
        (a, b) => a.state_version - b.state_version,
      ),
    })
  }
  private async post() {
    const owner = this.owner
    if (!owner?.pending) {
      this.set({ busy: false })
      return
    }
    const previousMode = this.state.reply?.mode
    const input: ChatInput = JSON.parse(owner.pending).input
    try {
      const result = await this.source.send(owner.pending, owner.session_token)
      // A replay from a prior version must never roll back a newer GET.
      if (result.state_version >= this.state.version) this.accept(result)
      owner.pending = null
      this.persist()
      this.set({ pending: false })
      if (result.state_version < this.state.version) {
        this.set({ reply: null, messages: [], ready: false })
        await this.read()
      }
      if (input.type === 'select_mode' && result.reply.mode === input.mode)
        this.set({
          notice:
            previousMode !== result.reply.mode
              ? 'Режим переключён сервером. Диагностический прогресс сброшен.'
              : 'Режим подтверждён сервером. Текущий прогресс сохранён.',
        })
    } catch (error) {
      if (this.unavailable(error)) return
      if (error instanceof ChatError && error.status === 409) {
        owner.pending = null
        this.persist()
        const message =
          error.code === 'STATE_CONFLICT'
            ? 'Состояние изменилось. Перечитываем сессию. Выберите действие заново.'
            : 'Сервер отклонил повтор из-за конфликта идентичности запроса. Перечитайте сессию и выберите действие заново.'
        this.set({
          pending: false,
          ready: false,
          reply: null,
          messages: [],
          next: null,
          notice: message,
        })
        if (error.code === 'STATE_CONFLICT') {
          try {
            await this.read()
          } catch (readError) {
            if (!this.unavailable(readError))
              this.set({
                error: 'Не удалось перечитать сессию. Повторите загрузку.',
              })
          }
        }
      } else if (
        error instanceof ChatError &&
        error.status >= 400 &&
        error.status < 500
      ) {
        owner.pending = null
        this.persist()
        this.set({
          pending: false,
          ready: false,
          reply: null,
          messages: [],
          next: null,
          error:
            'Запрос отклонён. Перечитайте сессию перед следующим действием.',
        })
      } else {
        this.set({
          error:
            'Исход запроса неизвестен. Повтор отправит тот же запрос без второго перехода.',
        })
      }
    } finally {
      this.finish()
    }
  }
}
