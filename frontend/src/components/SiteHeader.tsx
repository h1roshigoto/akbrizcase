import { useEffect, useState } from 'react'
import briz from '../design/assets/logo-briz.png'
import brizLight from '../design/assets/logo-briz-light.png'
import kemz from '../design/assets/logo-kemz.png'
import kemzLight from '../design/assets/logo-kemz-light.png'

export type PortalView = 'home' | 'catalog' | 'chat' | 'wizard' | 'request' | 'support' | 'docs' | 'tech'
export type PortalHash = `#${PortalView}` | '#selection'

export function SiteHeader({ view, navigate }: {
  view: PortalView
  count: number
  navigate: (hash: PortalHash) => void
  onSupport: () => void
  onCart: () => void
}) {
  const [menu, setMenu] = useState(false)
  const [dark, setDark] = useState(() => {
    try { const saved = localStorage.getItem('theme') ?? localStorage.getItem('mtdka-theme'); return saved === 'dark' || (saved !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches) } catch { return window.matchMedia('(prefers-color-scheme: dark)').matches }
  })
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  }, [dark])
  useEffect(() => { setMenu(false) }, [view])
  const links = [
    ['chat', 'Помощник'], ['catalog', 'Запчасти'],
    ['docs', 'Нормативная база'], ['tech', 'Техинформация'], ['request', 'Заявка'],
  ] as const
  return <header className="hdr site-header">
    <div className="wrap">
      <a className="logo" href="#home" aria-label="АК Бриз — на главную" onClick={() => navigate('#home')}>
        <img className="on-light" src={briz} alt="АК Бриз" width="134" height="120" />
        <img className="on-dark" src={brizLight} alt="АК Бриз" width="134" height="120" />
      </a>
      <nav id="portal-nav" className={`nav ${menu ? 'open' : ''}`} aria-label="Основное" onKeyDown={e => { if (e.key === 'Escape') { setMenu(false); document.getElementById('menu-toggle')?.focus() } }}>
        {links.map(([key, label]) => <a key={key} data-t={label} href={`#${key}`} aria-current={view === key || (key === 'chat' && view === 'wizard') ? 'page' : undefined} onClick={() => { navigate(`#${key}`); setMenu(false) }}>{label}</a>)}
        <a data-t="Контакты" href="#home" onClick={() => { navigate('#home'); setMenu(false); requestAnimationFrame(() => document.getElementById('contacts')?.scrollIntoView()) }}>Контакты</a>
      </nav>
      <button className="theme" type="button" aria-label="Тёмная тема" aria-pressed={dark} onClick={() => { setDark(!dark); try { localStorage.setItem('theme', dark ? 'light' : 'dark') } catch { /* Theme is optional. */ } }} title="Переключить тему">
        <svg className="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5" /><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8" /></svg>
        <svg className="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z" /></svg>
      </button>
      <a className="kemz" href="#home" onClick={() => navigate('#home')} aria-label="КЭМЗ"><img className="on-light" src={kemz} alt="КЭМЗ" width="138" height="40" /><img className="on-dark" src={kemzLight} alt="КЭМЗ" width="138" height="40" /></a>
      <button id="menu-toggle" className="burger" aria-label="Меню" aria-expanded={menu} aria-controls="portal-nav" onClick={() => setMenu(!menu)}><span /></button>
    </div>
  </header>
}

export function SiteFooter() {
  return <footer className="ftr"><div className="wrap">
    <div className="t"><span>ООО «АК Бриз»</span></div>
    <div className="b"><span>Сервис и запасные части</span><nav aria-label="Подвал">
      <a href="#home">Главная</a><a href="#chat">Помощник</a><a href="#home" onClick={() => requestAnimationFrame(() => document.getElementById('contacts')?.scrollIntoView())}>Контакты</a>
      <a href="/privacy-policy" target="_blank" rel="noopener noreferrer">Политика обработки ПДн</a>
      <a href="/admin/">Вход для сотрудников</a>
    </nav></div>
  </div></footer>
}
