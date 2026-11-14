import type { DocumentPage, DocumentQuery, KnowledgePage } from './types'
import { isSource, record, uuid } from './httpChat'

type PublicDocument = DocumentPage['items'][number]
const integer = (v: unknown) => Number.isSafeInteger(v) && Number(v) >= 0
export function documentURL(document: Pick<PublicDocument, 'id' | 'document_version_id' | 'url'>): string | null {
  return uuid(document.id) && uuid(document.document_version_id) && document.url === `/api/v1/documents/${document.id}/versions/${document.document_version_id}/file` ? document.url : null
}
function isDocument(v: unknown): v is PublicDocument {
  return record(v) && uuid(v.id) && uuid(v.document_version_id) &&
    ['code', 'title', 'url'].every(k => typeof v[k] === 'string' && (v[k] as string).length > 0) &&
    ['manual', 'regulation', 'article', 'other'].includes(String(v.kind)) &&
    v.visibility === 'public' && integer(v.version) && Number(v.version) >= 1 &&
    documentURL(v as PublicDocument) !== null
}
function page(v: unknown): v is Record<'items', unknown[]> & Record<'total' | 'offset' | 'limit', number> {
  return record(v) && Array.isArray(v.items) && v.items.length <= 100 && integer(v.total) && integer(v.offset) && integer(v.limit) && Number(v.limit) >= 1 && Number(v.limit) <= 100
}
export function isDocumentPage(v: unknown): v is DocumentPage {
  return page(v) && v.items.every(isDocument)
}
export function isKnowledgePage(v: unknown): v is KnowledgePage {
  return page(v) && v.limit <= 5 && v.items.length <= v.limit && v.items.length <= v.total && v.items.every(hit => {
    return record(hit) && typeof hit.excerpt === 'string' && isSource(hit.source)
  })
}
async function get<T>(path: string, signal: AbortSignal, validate: (v: unknown) => v is T): Promise<T> {
  // Always anonymous: a staff cookie must not turn the public library into an
  // accidental internal-document publishing surface. File routes still enforce ACL.
  const response = await fetch(`/api/v1/${path}`, { signal, credentials: 'omit', cache: 'no-store', headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error('Материалы недоступны.')
  const value: unknown = await response.json()
  if (!validate(value)) throw new Error('Некорректный ответ материалов.')
  return value
}
export const httpKnowledge = {
  documents(query: DocumentQuery, signal: AbortSignal) {
    const params = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) params.set(k, String(v))
    return get(`documents?${params}`, signal, isDocumentPage)
  },
  search(q: string, offset: number, signal: AbortSignal) {
    return get(`knowledge/search?${new URLSearchParams({ q, limit: '5', offset: String(offset) })}`, signal, isKnowledgePage)
  },
}
