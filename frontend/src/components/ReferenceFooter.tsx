import type { MouseEvent } from 'react'
import type { PortalHash } from './SiteHeader'

export function followPortalLink(event: MouseEvent<HTMLAnchorElement>, navigate: () => void) {
  if (event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
  event.preventDefault()
  navigate()
}

export function ReferenceFooter({ navigate, onContacts }: {
  navigate: (hash: PortalHash) => void
  onContacts: () => void
}) {
  return (
<footer className="ftr">
  <div className="wrap">
    <div className="t"><span>ООО «АК Бриз»</span></div>
    <div className="b"><span><a href="mailto:1968@bk.ru">1968@bk.ru</a><a href="tel:+79094265396">+7 (909) 426-53-96</a></span>
      <nav aria-label="Подвал"><a href="index.html" onClick={(event) => followPortalLink(event, () => navigate('#home'))}>Главная</a><a href="pomoshchnik.html" onClick={(event) => followPortalLink(event, () => navigate('#chat'))}>Помощник</a><a href="index.html#contacts" onClick={(event) => followPortalLink(event, onContacts)}>Контакты</a></nav>
    </div>
  </div>
</footer>
  )
}
