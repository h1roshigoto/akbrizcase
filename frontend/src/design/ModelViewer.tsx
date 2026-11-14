import { createElement, useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import './mtdka-viewer.js'
import model from './assets/mtdka.glb?url'
import poster from './assets/mtdka-poster.png'
import { httpKnowledge } from '../api/httpKnowledge'
import { useResource } from '../state/useResource'
import { SourceLink } from '../chat/SourceLink'

type Viewer = HTMLElement & { hold(): void; release(): void; pose(degrees: number | null): void; draw(): void; onframe: (() => void) | null }
const points = [
  { id: 'panel', name: 'Панель индикации', pos: '0 2.045 0.136', normal: '0 0 1', query: 'Панель индикации', link: '#tech', action: 'Попробовать на тренажёре' },
  { id: 'zones', name: 'Зоны обнаружения', pos: '0.36 1.45 0', normal: '-1 0 0', query: 'Зоны обнаружения', link: '#wizard', action: 'Не видит металл в отдельных зонах' },
  { id: 'sensors', name: 'Датчики прохода', pos: '-0.36 0.86 0.17', normal: '1 0 0', query: 'Датчики прохода', link: '#wizard', action: 'Не считает проходы' },
  { id: 'power', name: 'Разъём питания', pos: '-0.433 0.27 0.118', normal: '-1 0 0', query: 'Питание', link: '#wizard', action: 'Не включается' },
]
function PublishedNode({ query }: { query: string }) {
  const [attempt, setAttempt] = useState(0)
  const load = useCallback((signal: AbortSignal) => httpKnowledge.search(query, 0, signal), [query, attempt])
  const result = useResource(load)
  if (result.status === 'loading') return <p role="status">Загружаем источник…</p>
  if (result.status === 'error') return <p role="alert">Не удалось загрузить источник. <button className="link" onClick={() => setAttempt(n => n+1)}>Повторить</button></p>
  const hit = result.data.items[0]
  return hit ? <><p>{hit.excerpt}</p><p className="by">Источник: <SourceLink source={hit.source} /></p></> : <p>Материал для этого узла пока не опубликован.</p>
}
export function ModelViewer() {
  const viewer = useRef<Viewer>(null)
  const stage = useRef<HTMLDivElement>(null)
  const spot = useRef<HTMLDivElement>(null)
  const pop = useRef<HTMLDivElement>(null)
  const [point, setPoint] = useState<number | null>(null)
  const [dims, setDims] = useState(false)
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 600px)').matches)
  const close = (focus = false) => {
    if (focus && point !== null) document.querySelector<HTMLButtonElement>(`[aria-controls="hs-${points[point]!.id}"]`)?.focus()
    setPoint(null)
  }
  useEffect(() => {
    const media = window.matchMedia('(max-width: 600px)')
    const changed = () => setMobile(media.matches)
    media.addEventListener('change', changed)
    return () => media.removeEventListener('change', changed)
  }, [])
  useEffect(() => {
    if (point === null) return
    const v = viewer.current!
    v.hold()
    const outside = (e: MouseEvent) => { if (!(e.target as Element).closest('.hs-pop,.hs-b')) close() }
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.defaultPrevented) {e.preventDefault(); close(true)} }
    document.addEventListener('click', outside)
    document.addEventListener('keydown', escape)
    if (mobile) pop.current?.focus()
    const place = () => {
      if (mobile || !pop.current || !spot.current || !stage.current) return
      const S = stage.current.getBoundingClientRect(), a = spot.current.getBoundingClientRect(), p = pop.current
      const w = p.offsetWidth, h = p.offsetHeight, m = 10
      const left = Math.max(S.left+m-a.left, Math.min(18,S.right-m-w-a.left))
      let top = a.top+24+h <= S.bottom-m ? 24 : a.top-h-24 >= S.top+m ? -h-24 : Math.max(S.top+m-a.top, Math.min(-h/2,S.bottom-m-h-a.top))
      p.style.left = `${left}px`; p.style.top = `${top}px`
    }
    v.onframe = place
    place()
    return () => {v.onframe=null;v.release();document.removeEventListener('click',outside);document.removeEventListener('keydown',escape)}
  }, [point, mobile])
  const pointView = point === null ? null : <div ref={pop} className={`hs-pop ${mobile ? 'sheetpop' : ''}`} id={`hs-${points[point]!.id}`} role="dialog" aria-label={points[point]!.name} tabIndex={-1}>
    <button className="x" type="button" aria-label="Закрыть" onClick={() => close(true)}>×</button><b>{points[point]!.name}</b><PublishedNode key={points[point]!.id} query={points[point]!.query} /><a className="link" href={points[point]!.link}>{points[point]!.action}</a>
  </div>
  return <div className="stagewrap"><div className={`stage ${dims ? 'drafting' : ''}`} ref={stage}>
    <div className="sheet" aria-hidden="true" />
    {createElement('mtdka-viewer', { ref: viewer, className: 'm3d', src: model, speed:'-0.2',start:'25',tilt:'13','respect-motion':'',label:'3D-модель металлодетектора МТД-КА, вращается; можно повернуть мышью' },
      <img key="poster" src={poster} alt="Арочный металлодетектор МТД-КА" width="349" height="815" fetchPriority="high" />,
      ...points.map((p,i) => <div ref={point===i ? spot : undefined} key={p.id} slot="hotspot" className="hs" data-pos={p.pos} data-normal={p.normal} style={point===i ? {zIndex:5} : undefined}>
        <button className="hs-b" type="button" aria-expanded={point===i} aria-controls={`hs-${p.id}`} onClick={()=>setPoint(point===i?null:i)}><span className="sr">{p.name}</span></button>
        {point===i && !mobile && pointView}
      </div>),
      <svg key="dims" data-dims="" className="dims" aria-hidden="true" ref={svg=>{svg?.toggleAttribute('hidden',!dims)}}>
        <defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M0 1.5 10 5 0 8.5z" /></marker></defs>
        <line className="ext" data-a="0.44 0 0.29" data-b="0.56 0 0.29" /><line className="ext" data-a="0.36 2.23 0.29" data-b="0.56 2.23 0.29" /><line className="dim" data-a="0.52 0 0.29" data-b="0.52 2.23 0.29" />
        <line className="ext" data-a="-0.44 0 0.29" data-b="-0.44 0 0.46" /><line className="ext" data-a="0.44 0 0.29" data-b="0.44 0 0.46" /><line className="dim" data-a="-0.44 0 0.42" data-b="0.44 0 0.42" />
        <line className="ext" data-a="0.47 0 -0.29" data-b="0.66 0 -0.29" /><line className="ext" data-a="0.47 0 0.29" data-b="0.66 0 0.29" /><line className="dim" data-a="0.62 0 -0.29" data-b="0.62 0 0.29" />
        <line className="pass" data-a="-0.36 1.0 0.24" data-b="0.36 1.0 0.24" /><line className="pass" data-a="-0.24 0 0.24" data-b="-0.24 2.0 0.24" />
        <text data-at="0.6 1.1 0.29">H</text><text data-at="0 0 0.5">W</text><text data-at="0.76 0 0">D</text>
      </svg>)}
  </div><div className="stage-ctl"><button className="tgl" id="dimsBtn" type="button" aria-pressed={dims} onClick={()=>{setDims(!dims);viewer.current?.pose(dims?null:-30);viewer.current?.draw()}}>{dims?'Скрыть габариты':'Показать габариты'}</button><p className="tip" id="dimsCap">{dims?'Фактические размеры — в опубликованной документации':'Точки на модели — подсказки по узлам'}</p></div>
  {mobile && pointView && createPortal(pointView,document.body)}
  </div>
}
