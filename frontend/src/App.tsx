import { fixtureCatalog } from './api/fixtureCatalog'
import { CatalogPage } from './pages/CatalogPage'

// Stage 1 is explicitly fixture-only. Switch the composition root to httpCatalog
// after MVP-007 and verify real API/PostgreSQL requests through Compose.
export default function App() {
  return <CatalogPage source={fixtureCatalog} />
}
