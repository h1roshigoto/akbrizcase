import type { FormEvent } from 'react'

export function maskPhone(event: FormEvent<HTMLInputElement>) {
  const input = event.currentTarget
  input.value = formatReferencePhone(input.value)
}

export function formatReferencePhone(value: string) {
  let digits = value.replace(/\D/g, '')
  if (!digits) return ''
  if (digits[0] === '8') digits = '7' + digits.slice(1)
  if (digits[0] !== '7') digits = '7' + digits
  digits = digits.slice(1, 11)
  const [area, prefix, pair, end] = [digits.slice(0, 3), digits.slice(3, 6), digits.slice(6, 8), digits.slice(8)]
  return '+7' + (area ? ' (' + area : '') + (area.length === 3 ? ')' : '') +
    (prefix ? ' ' + prefix : '') + (pair ? '-' + pair : '') + (end ? '-' + end : '')
}

export function validateReferenceForm(form: HTMLFormElement) {
  let first: HTMLElement | undefined
  for (const field of form.querySelectorAll<HTMLInputElement>('[data-req]')) {
    const filled = field.type === 'checkbox' ? field.checked : Boolean(field.value.trim())
    const valid = filled &&
      (field.type !== 'tel' || field.value.replace(/\D/g, '').length === 11) &&
      (field.type !== 'email' || /^\S+@\S+\.\S+$/.test(field.value))
    field.classList.toggle('bad', !valid)
    field.setAttribute('aria-invalid', String(!valid))
    if (!valid) first ??= field
  }
  first?.focus()
  return !first
}
