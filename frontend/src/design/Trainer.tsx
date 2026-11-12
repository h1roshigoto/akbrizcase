import { useEffect, useRef, useState } from 'react'

/** A synthetic teaching animation, with no diagnostic tree or equipment commands. */
export function Trainer() {
  const [model, setModel] = useState('01')
  const [on, setOn] = useState(false)
  const [zone, setZone] = useState(3)
  const [passes, setPasses] = useState(0)
  const [alarms, setAlarms] = useState(0)
  const [alarm, setAlarm] = useState(false)
  const [walking, setWalking] = useState(false)
  const [sound, setSound] = useState(false)
  const audio = useRef<AudioContext | null>(null)
  useEffect(() => {
    if (!walking) return
    const timer = setTimeout(() => setWalking(false), 900)
    return () => clearTimeout(timer)
  }, [walking])
  useEffect(() => () => { void audio.current?.close() }, [])
  const reset = () => { setOn(false); setPasses(0); setAlarms(0); setAlarm(false); setWalking(false) }
  const walk = (metal: boolean) => {
    if (!on || walking) return
    setWalking(true); setPasses(n => n + 1); setAlarm(metal)
    if (metal) {
      setAlarms(n => n + 1)
      if (sound) {
        try {
          const ac = audio.current ?? (audio.current = new AudioContext())
          void ac.resume()
          const oscillator = ac.createOscillator(), gain = ac.createGain()
          oscillator.frequency.value = 660
          gain.gain.setValueAtTime(0.03, ac.currentTime)
          gain.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + 0.3)
          oscillator.connect(gain).connect(ac.destination)
          oscillator.start(); oscillator.stop(ac.currentTime + 0.3)
          oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
        } catch { setSound(false) }
      }
    }
  }
  return <section className="trainer" aria-labelledby="trainer-title">
    <h2 id="trainer-title">Тренажёр панели</h2>
    <p className="training-notice">Учебный режим · синтетическая симуляция. Не управляет оборудованием. Расположение зон и последовательность сигналов условные; это не инструкция по проверке или ремонту МТД-КА.</p>
    <fieldset className="tr-mode"><legend>Визуальный макет</legend>{['01', '18'].map(m => <label key={m}><input type="radio" name="trainer-model" checked={model === m} onChange={() => { reset(); setModel(m) }} /> МТД-КА-{m}</label>)}</fieldset>
    <div className="tr-grid"><figure className="tr-arch"><svg viewBox="0 0 1000 2400" role="img" aria-label="Учебный макет арки">
      <rect x="0" y="2300" width="1000" height="14" className="floor" />
      <rect x="140" y="300" width="720" height="2000" className="passage" />
      <g className={`person ${walking ? 'go' : ''}`}><ellipse cx="500" cy="655" rx="92" ry="112" className="body" /><path d="M400 790 L340 1350 L380 1370 L425 1000 L410 1650 L390 2270 L470 2270 L500 1670 L530 2270 L610 2270 L590 1650 L575 1000 L620 1370 L660 1350 L600 790 Z" className="body" /></g>
      {[70, 860].map(x => <g key={x}><rect x={x} y="135" width="70" height="2110" rx="4" className="post" /><rect x={x - 18} y="2245" width="106" height="55" rx="3" className="foot" /></g>)}
      <rect x="140" y="150" width="720" height="220" rx="4" className="beam" /><rect x="330" y="180" width="340" height="150" rx="3" className="beam-p" />
      {[98, 888].flatMap(x => Array.from({ length: 6 }, (_, i) => <rect key={`${x}-${i}`} x={x} y={2164 - i * 290} width="14" height="262" rx="2" className={`zled ${on && alarm && zone === i + 1 ? 'on' : ''}`} />))}
      {on && alarm && <circle cx="500" cy={2200 - zone * 290} r="42" className="item show" />}
    </svg><figcaption className="tip">Иллюстрация учебного прохода</figcaption></figure>
      <div className="tr-side"><svg className="tr-panel" viewBox="0 0 400 200" role="img" aria-label="Учебная панель индикации">
        <rect width="400" height="200" rx="4" className="p-bg" /><rect x="222" y="12" width="166" height="176" rx="2" className="p-in" />
        <circle cx="26" cy="24" r="8" className={`led g ${on ? 'on' : ''}`} /><text x="40" y="29" className="p-t">ГОТОВ</text>
        <circle cx="112" cy="24" r="8" className={`led r ${on && alarm ? 'on' : ''}`} /><text x="126" y="29" className="p-t">ТРЕВОГА</text>
        {Array.from({ length: model === '18' ? 3 : 2 }, (_, col) => Array.from({ length: 6 }, (_, i) => <rect key={`${col}-${i}`} x={18 + col * (model === '18' ? 50 : 85)} y={168 - i * 23} width={model === '18' ? 42 : 66} height="13" rx="1" className={`led r zone ${on && alarm && zone === i + 1 ? 'on' : ''}`} />))}
        <text x="240" y="45" className="p-t">ПРОХОДЫ</text><text x="240" y="85" className="counter">{on ? String(passes).padStart(4, '0') : '----'}</text>
        <text x="240" y="125" className="p-t">ТРЕВОГИ</text><text x="240" y="165" className="counter">{on ? String(alarms).padStart(4, '0') : '----'}</text>
      </svg>
      <div className="tr-ctl"><button className="btn" onClick={() => { setOn(!on); setAlarm(false) }}>{on ? 'Выключить макет' : 'Включить макет'}</button><button className="btn ghost" disabled={!on || walking} onClick={() => walk(false)}>Пройти без предмета</button><button className="btn ghost" disabled={!on || walking} onClick={() => walk(true)}>Пройти с предметом</button></div>
      <div className="tr-h"><label htmlFor="trainer-zone">Условная зона: {zone}</label><input id="trainer-zone" type="range" min="1" max="6" value={zone} onChange={e => setZone(Number(e.target.value))} /></div>
      <button className="sw" role="switch" aria-checked={sound} onClick={() => setSound(!sound)}><i aria-hidden="true" />Звук</button>
      <div className="tr-log" role="status"><p>{!on ? 'Учебный макет выключен.' : walking ? 'Учебный проход…' : alarm ? `Учебная тревога: условная зона ${zone}.` : 'Учебный макет готов.'}</p><p>Проходы: {passes} · Тревоги: {alarms}</p></div>
      <button className="text-button" onClick={reset}>Сбросить тренажёр</button>
    </div></div>
  </section>
}
