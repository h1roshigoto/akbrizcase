import { useEffect, useState } from 'react'

type Resource<T> =
  | { status: 'loading'; data: null }
  | { status: 'error'; data: null }
  | { status: 'success'; data: T }

export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
): Resource<T> {
  const [resource, setResource] = useState<
    Resource<T> & { loader: typeof load }
  >({
    loader: load,
    status: 'loading',
    data: null,
  })
  useEffect(() => {
    const controller = new AbortController()
    setResource({ loader: load, status: 'loading', data: null })
    void Promise.resolve()
      .then(() => load(controller.signal))
      .then(
        (data) => {
          if (!controller.signal.aborted)
            setResource({ loader: load, status: 'success', data })
        },
        () => {
          if (!controller.signal.aborted)
            setResource({ loader: load, status: 'error', data: null })
        },
      )
    return () => controller.abort()
  }, [load])
  // A changed request invalidates old data during render. Waiting for the
  // effect would briefly enable approval with the previous document/resource.
  return resource.loader === load ? resource : { status: 'loading', data: null }
}
