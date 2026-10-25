import { httpCatalog } from './api/httpCatalog'
import { CatalogPage } from './pages/CatalogPage'

export default function App() {
  return <CatalogPage source={httpCatalog} />
}
