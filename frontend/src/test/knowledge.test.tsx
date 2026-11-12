import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { documentURL, httpKnowledge, isDocumentPage, isKnowledgePage } from '../api/httpKnowledge'
import { fixtures } from '../api/fixtures'
import { DocumentsPage } from '../pages/DocumentsPage'

const source = fixtures.knowledge_page.items[0]!.source
const document = { id: source.document_id, document_version_id: source.document_version_id, url: source.url, title: source.title, code: 'SYNTHETIC', kind: 'manual' as const, visibility: 'public' as const, version: 1 }
const page = { items: [document], total: 1, limit: 20, offset: 0 }

it('uses anonymous, abortable, no-store document and search reads with encoded queries', async () => {
  const signal = new AbortController().signal
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json(page)).mockResolvedValueOnce(Response.json(fixtures.knowledge_page))
  vi.stubGlobal('fetch', fetcher)
  expect(await httpKnowledge.documents({ kind: 'manual', limit: 20, offset: 0 }, signal)).toEqual(page)
  expect(await httpKnowledge.search('тест & query', 20, signal)).toEqual(fixtures.knowledge_page)
  expect(fetcher.mock.calls[0]).toEqual(['/api/v1/documents?kind=manual&limit=20&offset=0', expect.objectContaining({ credentials: 'omit', cache: 'no-store', signal })])
  expect(fetcher.mock.calls[1]![0]).toContain('q=%D1%82%D0%B5%D1%81%D1%82+%26+query&limit=20&offset=20')
})

it.each([
  { ...document, visibility: 'internal' },
  { ...document, url: 'javascript:alert(1)' },
  { ...document, url: 'https://example.invalid/document' },
  { ...document, url: `/api/v1/documents/${source.document_id}/versions/00000000-0000-4000-8000-000000000009/file` },
  { ...document, version: 0 },
])('rejects closed material and hostile or mismatched document links %#', value => {
  expect(isDocumentPage({ ...page, items: [value] })).toBe(false)
})

it('rejects unsafe citations and malformed pagination without inventing document URLs', () => {
  expect(isKnowledgePage(fixtures.knowledge_page)).toBe(true)
  expect(isKnowledgePage({ ...fixtures.knowledge_page, items: [{ excerpt: 'test', source: { ...source, url: '//example.invalid/' } }] })).toBe(false)
  expect(isKnowledgePage({ ...fixtures.knowledge_page, total: -1 })).toBe(false)
  expect(documentURL({ ...document, id: 'bad' as typeof document.id })).toBeNull()
})

it('shows a real error, retries, escapes hostile content and only renders verified links', async () => {
  const user = userEvent.setup()
  const attack = '<img src=x onerror=alert(1)>'
  const fetcher = vi.fn().mockResolvedValueOnce(new Response('', { status: 503 })).mockResolvedValueOnce(Response.json({ ...page, items: [{ ...document, title: attack }] }))
  vi.stubGlobal('fetch', fetcher)
  const { container } = render(<DocumentsPage />)
  await screen.findByRole('alert')
  expect(container.querySelector('a[href^="/api/"]')).toBeNull()
  await user.click(screen.getByRole('button', { name: 'Повторить загрузку документов' }))
  await screen.findByRole('heading', { name: attack })
  expect(container.querySelector('[onerror], img, script')).toBeNull()
  expect(screen.getByRole('link', { name: 'Открыть документ' }).getAttribute('href')).toBe(source.url)
})

it('displays an honest empty library without bundled knowledge fallback', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ items: [], total: 0, limit: 20, offset: 0 })))
  render(<DocumentsPage />)
  await screen.findByText('Опубликованные материалы не найдены.')
  expect(screen.queryAllByRole('article')).toHaveLength(0)
})
