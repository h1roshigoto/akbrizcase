import { useEffect, useState } from 'react'

type Resource<T> =
  | { status: 'loading'; data: null }
  | { status: 'error'; data: null }
  | { status: 'success'; data: T }

export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
): Resource<T> {
  const [resource, setResource] = useState<Resource<T>>({
    status: 'loading',
    data: null,
  })
  useEffect(() => {
    const controller = new AbortController()
    setResource({ status: 'loading', data: null })
    void Promise.resolve()
      .then(() => load(controller.signal))
      .then(
        (data) => {
          if (!controller.signal.aborted)
            setResource({ status: 'success', data })
        },
        () => {
          if (!controller.signal.aborted)
            setResource({ status: 'error', data: null })
        },
      )
    return () => controller.abort()
  }, [load])
  return resource
}
