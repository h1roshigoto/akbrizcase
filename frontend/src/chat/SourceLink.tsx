import type { SourceRef } from '../api/types'
import { sourceURL } from '../api/httpChat'

export function SourceLink({
  source,
  disabled = false,
}: {
  source: SourceRef
  disabled?: boolean
}) {
  const url = sourceURL(source)
  const label = (
    <>
      {source.title} · {source.section}
      {source.page_from !== null &&
        ` · стр. ${source.page_from}–${source.page_to}`}
    </>
  )
  return url && !disabled ? (
    <a href={url} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  ) : (
    <span>{label}</span>
  )
}
