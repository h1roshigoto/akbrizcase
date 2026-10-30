import { httpCatalog } from './api/httpCatalog'
import { CatalogPage } from './pages/CatalogPage'
import { PrivacyPage } from './pages/PrivacyPage'

export default function App() {
  if (window.location.pathname === '/privacy-policy') return <PrivacyPage />
  return <CatalogPage source={httpCatalog} />
}
