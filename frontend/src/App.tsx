import { httpCatalog } from './api/httpCatalog'
import { CatalogPage } from './pages/CatalogPage'
import { PrivacyPage } from './pages/PrivacyPage'

export default function App() {
  if (window.location.pathname === '/privacy-policy') return <PrivacyPage />
  const pages = { '/index.html': 'home', '/zapchasti.html': 'catalog', '/pomoshchnik.html': 'chat', '/zayavka.html': 'request', '/tehinfo.html': 'tech', '/normy.html': 'docs' } as const
  return <CatalogPage source={httpCatalog} defaultView={pages[window.location.pathname as keyof typeof pages] ?? 'home'} />
}
