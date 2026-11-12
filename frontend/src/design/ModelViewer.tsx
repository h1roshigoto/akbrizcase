import { createElement, useEffect, useRef, useState } from 'react'
import './mtdka-viewer.js'
import model from './assets/mtdka.glb?url'
import poster from './assets/mtdka-poster.png'

type Viewer = HTMLElement & { hold(): void; release(): void; pose(degrees: number | null): void }
const points = [
  { name: 'Верхняя панель макета', pos: '0 2.04 0.14', text: 'Демонстрационная точка. Назначение индикаторов уточняйте в опубликованной документации.' },
  { name: 'Стойка макета', pos: '-0.39 1.1 0.17', text: 'Визуальная модель. Не используйте её как инструкцию по разборке или обслуживанию.' },
  { name: 'Основание макета', pos: '0.39 0.08 0.2', text: 'Для подбора деталей перейдите в каталог и проверьте подтверждённую применимость.' },
]

export function ModelViewer() {
  const ref = useRef<Viewer>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'poster'>('loading')
  const [paused, setPaused] = useState(false)
  const [dims, setDims] = useState(false)
  const [point, setPoint] = useState<number | null>(null)
  const [angle, setAngle] = useState(25)
  useEffect(() => {
    const viewer = ref.current!
    const load = () => setStatus('ready')
    const error = () => setStatus('poster')
    viewer.addEventListener('load', load)
    viewer.addEventListener('viewer-error', error)
    // A custom element can finish its synchronous no-WebGL fallback before effects.
    if (!viewer.querySelector('canvas')) setStatus('poster')
    return () => {
      viewer.removeEventListener('load', load)
      viewer.removeEventListener('viewer-error', error)
    }
  }, [])
  useEffect(() => {
    const viewer = ref.current!
    if (paused || point !== null) viewer.hold()
    else return
    return () => viewer.release()
  }, [paused, point])
  return <div className="stagewrap">
    <div className={`stage ${dims ? 'drafting' : ''}`}><div className="sheet" aria-hidden="true" />
      {createElement('mtdka-viewer', { ref, className: 'm3d', src: model, speed: '-0.2', start: '25', tilt: '13', 'respect-motion': '', label: '3D-макет МТД-КА' },
        <img src={poster} alt="Арочный металлодетектор МТД-КА — визуальный макет" width="349" height="815" />,
        ...points.map((p, i) => <div key={p.name} slot="hotspot" className="hs" data-pos={p.pos}>
          <button className="hs-b" type="button" aria-label={p.name} aria-expanded={point === i} aria-controls={`model-point-${i}`} onClick={() => setPoint(point === i ? null : i)} />
        </div>),
        <svg key="dims" data-dims="" className="dims" aria-hidden="true" ref={svg => { svg?.toggleAttribute('hidden', !dims) }}>
          <line className="ext" data-a="0.44 0 0.29" data-b="0.56 0 0.29" /><line className="ext" data-a="0.36 2.23 0.29" data-b="0.56 2.23 0.29" />
          <line className="dim" data-a="0.52 0 0.29" data-b="0.52 2.23 0.29" /><text data-at="0.6 1.1 0.29">H</text>
          <line className="dim" data-a="-0.44 0 0.42" data-b="0.44 0 0.42" /><text data-at="0 0 0.5">W</text>
          <line className="dim" data-a="0.62 0 -0.29" data-b="0.62 0 0.29" /><text data-at="0.76 0 0">D</text>
        </svg>)}
    </div>
    <div className="stage-ctl">
      <button className="tgl" aria-pressed={dims} disabled={status !== 'ready'} onClick={() => { setDims(!dims); ref.current?.pose(dims ? null : 25) }}>Показать габариты</button>
      <button className="tgl" aria-pressed={paused} disabled={status !== 'ready'} onClick={() => setPaused(!paused)}>Пауза вращения</button>
      <button className="tgl" disabled={status !== 'ready'} onClick={() => { const next = angle - 30; setAngle(next); ref.current?.pose(next); setPaused(true) }}>Повернуть влево</button>
      <button className="tgl" disabled={status !== 'ready'} onClick={() => { const next = angle + 30; setAngle(next); ref.current?.pose(next); setPaused(true) }}>Повернуть вправо</button>
      <button className="tgl" disabled={status !== 'ready'} onClick={() => { setDims(false); setPoint(null); setPaused(false); setAngle(25); ref.current?.pose(null) }}>Сбросить вид</button>
    </div>
    <p className="tip" role="status">{status === 'poster' ? '3D недоступно. Показано изображение макета.' : status === 'loading' ? 'Загрузка 3D-макета…' : 'Вращайте макет мышью или кнопками. Точки — демонстрационные подписи.'}</p>
    {dims && <p className="tip">H, W, D — оси макета. Фактические размеры уточняйте по опубликованным документам выбранной модели.</p>}
    {point !== null && <section className="model-point" id={`model-point-${point}`} aria-label={points[point]!.name} onKeyDown={e => { if (e.key === 'Escape') setPoint(null) }}><button className="x" aria-label="Закрыть подсказку" onClick={() => setPoint(null)}>×</button><b>{points[point]!.name}</b><p>{points[point]!.text}</p><a className="link" href="#docs">Документы и источники</a></section>}
  </div>
}
