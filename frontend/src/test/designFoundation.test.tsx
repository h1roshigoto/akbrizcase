import type { FormEvent } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { Button } from '../components/Button'
import { StatusPanel } from '../components/StatusPanel'

it('keeps actions inside forms from submitting unless submit is explicit', async () => {
  const user = userEvent.setup()
  const action = vi.fn()
  const submit = vi.fn((event: FormEvent) => event.preventDefault())
  render(<form onSubmit={submit}><Button onClick={action}>Действие</Button><Button type="submit" variant="primary">Найти</Button></form>)
  await user.tab()
  await user.keyboard('{Enter}')
  expect(action).toHaveBeenCalledTimes(1)
  expect(submit).not.toHaveBeenCalled()
  await user.tab()
  await user.keyboard(' ')
  expect(submit).toHaveBeenCalledTimes(1)
})

it('preserves native disabled behavior and accessible attributes', async () => {
  const action = vi.fn()
  render(<><Button disabled onClick={action}>Недоступно</Button><Button aria-pressed="true" aria-label="Переключить">Режим</Button></>)
  await userEvent.click(screen.getByRole('button', { name: 'Недоступно' }))
  expect(action).not.toHaveBeenCalled()
  await userEvent.tab()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Переключить', pressed: true }))
})

it('announces loading without exposing a decorative spinner or actions', () => {
  render(<StatusPanel kind="loading" onRetry={vi.fn()} onReset={vi.fn()} />)
  expect(screen.getByRole('status').textContent).toContain('Загружаем каталог')
  expect(screen.queryByRole('button')).toBeNull()
  expect(screen.queryByRole('img')).toBeNull()
})

it('shows recoverable errors as alerts and supports retry by keyboard', async () => {
  const retry = vi.fn()
  render(<StatusPanel kind="error" onRetry={retry} />)
  expect(screen.getByRole('alert').getAttribute('aria-atomic')).toBe('true')
  expect(screen.getByRole('alert').textContent).toContain('Не удалось загрузить данные')
  await userEvent.tab()
  await userEvent.keyboard('{Enter}')
  expect(retry).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('button', { name: 'Сбросить фильтры' })).toBeNull()
})

it('offers only the appropriate recovery action for empty results', async () => {
  const reset = vi.fn()
  render(<StatusPanel kind="empty" onReset={reset} onRetry={vi.fn()} />)
  expect(screen.getByRole('status').textContent).toContain('Позиции не найдены')
  await userEvent.click(screen.getByRole('button', { name: 'Сбросить фильтры' }))
  expect(reset).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Повторить' })).toBeNull()
})

it('does not offer an inert retry when no recovery action is available', () => {
  render(<StatusPanel kind="error" />)
  expect(screen.getByRole('alert')).toBeTruthy()
  expect(screen.queryByRole('button')).toBeNull()
})
