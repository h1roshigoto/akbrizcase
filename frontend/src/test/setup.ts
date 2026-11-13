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

// jsdom has no native modal implementation; real focus/inert/Escape are checked in Chromium.
HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }

Object.defineProperty(window, 'matchMedia', { writable: true, value: (query: string) => ({matches:false,media:query,onchange:null,addListener(){},removeListener(){},addEventListener(){},removeEventListener(){},dispatchEvent(){return false}}) })
