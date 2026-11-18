import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import {
  centerTarget, detectionFor, formatHeight, TrainerArch, TrainerPanel, type Target, type TrainerModel,
} from './TrainerGraphics'

type Phase = 'off' | 'connect' | 'standby' | 'start' | 'fault' | 'ready' | 'pass' | 'sensor' | 'result' | 'settle'
type Progress = {
  phase: Phase
  step: number
  passes: number
  alarms: number
  metal: boolean
  target: Target
  message: string
}
type Step = { rows: [string, string]; message: string; duration: number; zones?: number }

const coilCodes = ['r1', 'r2', 'r3', 'r4', 'r5', 'r6', 'L1', 'L2', 'L3', 'L4', 'L5', 'L6']
const startup01: Step[] = [
  { rows: ['----', '----'], message: 'Учебная автодиагностика запущена.', duration: 900 },
  ...['H5', 'L5', 'Ld', 'rd'].map((label): Step => ({
    rows: [label, '0'], message: 'Проверка элементов учебной панели.', duration: 550,
  })),
  ...coilCodes.map((label): Step => ({
    rows: [label, '0'], message: 'Имитация опроса катушек по стойкам.', duration: 220,
  })),
  ...[1, 2, 3, 4, 5, 6].map((zones): Step => ({
    rows: ['8888', '8888'], message: 'Проверка индикаторов.', duration: 260, zones,
  })),
]
const startup18 = [5, 4, 3, 2, 1].map((count): Step => ({
  rows: ['CAL', String(count)], message: 'Учебная калибровка. Дождитесь окончания отсчёта.', duration: 650,
}))
const readyMessage = 'Учебный макет готов. Выполните проход через рамку.'
const sideNames = { L: 'левая зона', C: 'центральная зона', R: 'правая зона' }

function initialProgress(model: TrainerModel): Progress {
  return {
    phase: 'off', step: 0, passes: 0, alarms: 0, metal: false, target: centerTarget,
    message: model === '01' ? 'Учебный макет выключен.' : 'Учебный макет отключён от сети.',
  }
}

function simulatePass(model: TrainerModel, progress: Progress): Progress {
  const { metal, target } = progress
  const detection = detectionFor(model, target)
  const area = detection.sides.map(side => sideNames[side]).join(' и ')
  return {
    ...progress,
    phase: 'result',
    passes: Math.min(9999, progress.passes + 1),
    alarms: Math.min(9999, progress.alarms + Number(metal)),
    message: metal ? 'Учебная тревога: зона ' + (detection.zone + 1) + ' (' + area + ').' : 'Проход засчитан, тревоги нет.',
  }
}

const presets = [
  [150, '15 см от пола'],
  [1000, '1 м от пола'],
  [1780, '15 см ниже перекладины'],
] as const

export function Trainer() {
  const [model, setModel] = useState<TrainerModel>('01')
  const [progress, setProgress] = useState(() => initialProgress('01'))
  const [target, setTarget] = useState<Target>(centerTarget)
  const [sound, setSound] = useState(true)
  const [fault, setFault] = useState(false)
  const [holding, setHolding] = useState(false)
  const holdTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const audio = useRef<AudioContext | null>(null)
  const { phase } = progress
  const startSteps = model === '01' ? startup01 : startup18
  const currentStep = phase === 'start' ? startSteps[progress.step] : undefined
  const passing = phase === 'pass' || phase === 'sensor' || phase === 'result' || phase === 'settle'
  const powered = phase === 'ready' || passing
  const busy = passing || phase === 'connect' || phase === 'start'
  const detection = (phase === 'result' || phase === 'settle') && progress.metal
    ? detectionFor(model, progress.target) : null

  useEffect(() => () => {
    if (holdTimer.current !== null) clearTimeout(holdTimer.current)
    void audio.current?.close().catch(() => {})
  }, [])

  useEffect(() => {
    let duration: number
    let advance: (previous: Progress) => Progress
    switch (phase) {
      case 'connect':
        duration = 1400
        advance = previous => ({ ...previous, phase: 'standby', message: 'Режим OFF. Нажмите «вкл» для учебной калибровки.' })
        break
      case 'start':
        if (!currentStep) return
        duration = currentStep.duration
        advance = previous => {
          const nextStep = previous.step + 1
          if (model === '01' && fault && startSteps[nextStep]?.rows[0] === 'L1') {
            return { ...previous, phase: 'fault', message: 'Имитация неисправности: катушка L1, индикация E. Учебный макет не перешёл в режим готовности.' }
          }
          if (nextStep < startSteps.length) return { ...previous, step: nextStep }
          return { ...previous, phase: 'ready', message: readyMessage }
        }
        break
      case 'pass':
        duration = 450
        advance = previous => ({ ...previous, phase: 'sensor' })
        break
      case 'sensor':
        duration = 300
        advance = previous => simulatePass(model, previous)
        break
      case 'result':
        duration = 600
        advance = previous => ({ ...previous, phase: 'settle' })
        break
      case 'settle':
        duration = 2500
        advance = previous => ({ ...previous, phase: 'ready', metal: false })
        break
      default:
        return
    }
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const delay = reduceMotion && phase !== 'settle' ? Math.min(duration, 60) : duration
    const timer = setTimeout(() => setProgress(previous => previous === progress ? advance(previous) : previous), delay)
    return () => clearTimeout(timer)
  }, [phase, currentStep, model, fault, startSteps, progress])

  useEffect(() => {
    const context = audio.current
    if (phase !== 'result' || !progress.metal || !sound || !context || context.state !== 'running') return
    for (const [frequency, delay] of [[880, 0], [659, .2]] as const) {
      const oscillator = context.createOscillator()
      const gain = context.createGain()
      const start = context.currentTime + delay
      oscillator.frequency.value = frequency
      gain.gain.setValueAtTime(.04, start)
      gain.gain.exponentialRampToValueAtTime(.001, start + .35)
      oscillator.connect(gain).connect(context.destination)
      oscillator.start(start)
      oscillator.stop(start + .35)
      oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
    }
  }, [phase, progress.metal, sound])

  function stopHold() {
    if (holdTimer.current === null) return false
    clearTimeout(holdTimer.current)
    holdTimer.current = null
    setHolding(false)
    return true
  }

  function releasePower() {
    if (stopHold()) setProgress(previous => ({ ...previous, message: 'Удерживайте кнопку одну секунду.' }))
  }

  function reset(next: TrainerModel) {
    stopHold()
    setModel(next)
    setProgress(initialProgress(next))
  }

  function beginPower() {
    if (busy || holdTimer.current !== null) return
    if (powered) {
      setProgress(model === '18'
        ? { ...initialProgress(model), phase: 'standby', message: 'Учебный макет вернулся в режим OFF.' }
        : initialProgress(model))
      return
    }
    if (model === '18') {
      setProgress({ ...initialProgress(model), phase: phase === 'off' ? 'connect' : 'start' })
      return
    }
    setHolding(true)
    holdTimer.current = setTimeout(() => {
      holdTimer.current = null
      setHolding(false)
      setProgress({ ...initialProgress(model), phase: 'start' })
    }, 1000)
  }

  function powerKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== ' ' && event.key !== 'Enter') return
    event.preventDefault()
    if (!event.repeat) beginPower()
  }

  function powerPointer(event: PointerEvent<HTMLButtonElement>) {
    if (event.button === 0) beginPower()
  }

  function walk(metal: boolean, position = target) {
    if (phase !== 'ready') return
    if (metal && sound && typeof AudioContext !== 'undefined') {
      try {
        const context = audio.current ?? (audio.current = new AudioContext())
        void context.resume().catch(() => {})
      } catch { setSound(false) }
    }
    setProgress(previous => previous.phase === 'ready'
      ? { ...previous, phase: 'pass', metal, target: position, message: 'Учебный проход…' }
      : previous)
  }

  const counters: [string, string] = model === '01'
    ? [String(progress.passes), String(progress.alarms)]
    : [String(progress.alarms), String(progress.passes)]
  const rows: [string, string] = currentStep?.rows ?? (
    powered ? counters : phase === 'fault' ? ['L1', 'E'] : phase === 'standby' || phase === 'connect' ? ['OFF', ''] : ['', '']
  )
  const message = currentStep?.message ?? (phase === 'connect' ? 'Учебный макет подключён. На панели OFF.' : progress.message)
  const powerLabel = model === '01'
    ? powered ? 'Выключить' : 'Включить'
    : powered ? 'Нажать «вкл» (в режим OFF)' : phase === 'off' ? 'Подключить к сети' : 'Нажать «вкл»'
  const powerHint = model === '01' ? !powered : phase === 'off'

  return <section className="trainer" id="trainer" aria-labelledby="trainer-title">
    <h2 id="trainer-title">Тренажёр панели МТД-КА-{model}</h2>
    <p className="tr-lead">Выберите модификацию, включите учебный макет и выполните проход. Панель покажет проходы, тревоги и условную зону. Фактические параметры приведены в опубликованной документации.</p>
    <div className="choice sm tr-mode" role="radiogroup" aria-label="Модификация">
      {(['01', '18'] as const).map(value => <label key={value}>
        <input type="radio" name="trainer-model" checked={model === value} onChange={() => reset(value)} />
        <span>МТД-КА-{value}</span>
      </label>)}
    </div>
    <div className="tr-grid">
      <TrainerArch target={passing ? progress.target : target} detection={detection}
        litZones={phase === 'connect' ? 6 : currentStep?.zones ?? 0}
        walking={phase === 'pass' || phase === 'sensor' || phase === 'result'} sensors={phase === 'sensor'}
        metal={passing && progress.metal}
        onTarget={position => { setTarget(position); walk(true, position) }} />
      <div className="tr-side">
        <TrainerPanel model={model} rows={rows} ready={powered} detection={detection}
          litZones={currentStep?.zones ?? 0} testingIndicators={currentStep?.zones === 6} />
        <div className="tr-ctl">
          <button className={'btn tr-power' + (holding ? ' holding' : '') + (powered ? ' ghost' : '')}
            type="button" disabled={busy} aria-describedby={powerHint ? 'tr-hint' : undefined}
            onPointerDown={powerPointer} onPointerUp={releasePower} onPointerLeave={releasePower}
            onPointerCancel={releasePower} onBlur={releasePower} onKeyDown={powerKey} onKeyUp={event => {
              if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); releasePower() }
            }} onClick={event => { if (event.detail === 0 && !holding) beginPower() }}>
            <span className="fill" aria-hidden="true" /><span className="lbl">{powerLabel}</span>
          </button>
          <button className="btn ghost" disabled={phase !== 'ready'} onClick={() => walk(false)}>Пройти без металла</button>
          <button className="btn ghost" disabled={phase !== 'ready'} onClick={() => walk(true)}>Пройти с предметом</button>
        </div>
        {powerHint && <p className="tip" id="tr-hint">{model === '01'
          ? 'Чтобы включить макет, удерживайте кнопку одну секунду.'
          : 'Сначала подключите учебный макет к сети, затем нажмите «вкл».'}</p>}
        <div className="tr-h">
          <label htmlFor="tr-height">Высота предмета</label>
          <input id="tr-height" type="range" min="50" max="1950" step="10" value={target.height}
            onChange={event => setTarget({ ...target, height: Number(event.target.value) })} />
          <output htmlFor="tr-height">{formatHeight(target.height)}</output>
        </div>
        <div className="chips tr-pre" role="group" aria-label="Высота учебного предмета">
          {presets.map(([height, label]) => <button className="chip" key={height}
            onClick={() => setTarget({ height, x: centerTarget.x })}>{label}</button>)}
        </div>
        <div className="tr-opts">
          <button className="sw" role="switch" aria-checked={sound} onClick={() => setSound(!sound)}><i aria-hidden="true" />Звук</button>
          {model === '01' && <button className="sw" role="switch" aria-checked={fault}
            disabled={busy || powered} onClick={() => setFault(!fault)}><i aria-hidden="true" />Неисправная катушка</button>}
          <button className="text-button" onClick={() => { reset(model); setTarget(centerTarget) }}>Сбросить тренажёр</button>
        </div>
        <div className="tr-log" role="status">
          <p className="tr-now">{message}</p><p className="by">Проходы: {progress.passes} · Тревоги: {progress.alarms}</p>
        </div>
      </div>
    </div>
  </section>
}
