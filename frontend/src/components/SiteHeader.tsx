import { useEffect, useRef, useState } from 'react'
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
  const header = useRef<HTMLElement>(null)
  const [menu, setMenu] = useState(false)
  const [dark, setDark] = useState(() => {
    try { const saved = localStorage.getItem('theme') ?? localStorage.getItem('mtdka-theme'); return saved === 'dark' || (saved !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches) } catch { return window.matchMedia('(prefers-color-scheme: dark)').matches }
  })
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  }, [dark])
  useEffect(() => { setMenu(false) }, [view])
  useEffect(() => {
    if (menu) header.current?.querySelector<HTMLAnchorElement>('.nav a')?.focus({ preventScroll: true })
  }, [menu])
  useEffect(() => {
    const element = header.current
    const updateBorder = () => element?.classList.toggle('stuck', window.scrollY > 8)
    updateBorder()
    window.addEventListener('scroll', updateBorder, { passive: true })
    return () => window.removeEventListener('scroll', updateBorder)
  }, [])
  const links = [
    ['chat', 'Помощник'], ['catalog', 'Запчасти'],
    ['docs', 'Нормативная база'], ['tech', 'Техинформация'], ['request', 'Заявка'],
  ] as const
  const reference = view === 'request'
  const referencePages = {
    chat: 'pomoshchnik.html',
    catalog: 'zapchasti.html',
    docs: 'normy.html',
    tech: 'tehinfo.html',
    request: 'zayavka.html',
  }
  const follow = (event: React.MouseEvent<HTMLAnchorElement>, hash: PortalHash) => {
    if (event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    event.preventDefault()
    navigate(hash)
    setMenu(false)
  }
  return <header className={reference ? 'hdr' : 'hdr site-header'} ref={header}>
    <div className="wrap">
      <a className="logo" href={reference ? 'index.html' : '#home'} aria-label="АК Бриз — на главную" onClick={(event) => follow(event, '#home')}>
        <img className="on-light" src={briz} alt="АК Бриз" width="134" height="120" />
        <img className="on-dark" src={brizLight} alt="АК Бриз" width="134" height="120" />
      </a>
      <nav id="portal-nav" className={menu ? 'nav open' : 'nav'} aria-label="Основное" onKeyDown={e => { if (e.key === 'Escape') { setMenu(false); document.getElementById('menu-toggle')?.focus() } }}>
        {links.map(([key, label]) => <a key={key} data-t={label} href={reference ? referencePages[key] : `#${key}`} aria-current={view === key || (key === 'chat' && view === 'wizard') ? 'page' : undefined} onClick={(event) => follow(event, `#${key}`)}>{label}</a>)}
        <a data-t="Контакты" href={reference ? 'index.html#contacts' : '#home'} onClick={(event) => { follow(event, '#home'); if (event.defaultPrevented) requestAnimationFrame(() => document.getElementById('contacts')?.scrollIntoView()) }}>Контакты</a>
      </nav>
      <button className="theme" type="button" aria-label="Тёмная тема" aria-pressed={dark} onClick={() => { setDark(!dark); try { localStorage.setItem('theme', dark ? 'light' : 'dark') } catch { /* Theme is optional. */ } }} title="Переключить тему">
        <svg className="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5" /><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8" /></svg>
        <svg className="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z" /></svg>
      </button>
      <a className="kemz" href={reference ? 'index.html' : '#home'} onClick={(event) => follow(event, '#home')} aria-label="КЭМЗ"><img className="on-light" src={kemz} alt="КЭМЗ" width="138" height="40" /><img className="on-dark" src={kemzLight} alt="КЭМЗ" width="138" height="40" /></a>
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
