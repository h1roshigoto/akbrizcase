import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { httpOrders, type OrdersSource } from '../api/httpOrders'
import { PrivacyPage } from '../pages/PrivacyPage'
import { policyFixture } from './privacyFixture'

it('opens the policy independently without submitting an order', async () => {
  const api: OrdersSource = {
    getPolicy: vi.fn().mockResolvedValue(policyFixture),
    getConsent: vi.fn(),
    submit: vi.fn(),
  }
  render(<PrivacyPage orders={api} />)
  await screen.findByText(policyFixture.text)
  expect(screen.getByText(policyFixture.withdrawal_instructions)).toBeTruthy()
  expect(screen.getByText(/Только синтетическое демо/)).toBeTruthy()
  expect(api.submit).not.toHaveBeenCalled()
  expect(api.getConsent).not.toHaveBeenCalled()
})

it('fails closed for an unavailable public policy', async () => {
  const api: OrdersSource = {
    getPolicy: vi.fn().mockRejectedValue(new Error('down')),
    getConsent: vi.fn(),
    submit: vi.fn(),
  }
  render(<PrivacyPage orders={api} />)
  await screen.findByRole('alert')
  expect(api.submit).not.toHaveBeenCalled()
})

it('loads a same-origin policy without caching and rejects malformed responses', async () => {
  const fetcher = vi.fn().mockResolvedValue(Response.json(policyFixture))
  vi.stubGlobal('fetch', fetcher)
  const signal = new AbortController().signal
  expect(await httpOrders.getPolicy(signal)).toEqual(policyFixture)
  expect(fetcher).toHaveBeenCalledWith(
    '/api/v1/privacy-policy',
    expect.objectContaining({
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      signal,
    }),
  )
  for (const invalid of [
    {},
    { ...policyFixture, synthetic: 'false' },
    { ...policyFixture, checksum: 'bad' },
    { ...policyFixture, withdrawal_instructions: '' },
  ]) {
    fetcher.mockResolvedValueOnce(Response.json(invalid))
    await expect(httpOrders.getPolicy(signal)).rejects.toThrow()
  }
})
