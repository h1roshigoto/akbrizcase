import type { DecimalString } from '../api/types'

/** Decimal strings stay exact, including values beyond Number.MAX_SAFE_INTEGER. */
export function toKopecks(price: DecimalString): bigint {
  if (!/^\d+(\.\d{1,2})?$/.test(price)) throw new Error('Invalid decimal price')
  const [whole = '0', fraction = ''] = price.split('.')
  return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))
}

export function formatKopecks(value: bigint): string {
  const whole = (value / 100n)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0')
  return `${whole},${(value % 100n).toString().padStart(2, '0')}`
}

export const formatPrice = (price: DecimalString): string =>
  formatKopecks(toKopecks(price))
