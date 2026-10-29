import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

beforeEach(() => {
  sessionStorage.clear()
  window.history.replaceState(null, '', window.location.pathname)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
