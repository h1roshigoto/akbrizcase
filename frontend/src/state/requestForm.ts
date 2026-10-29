export type FormValues = {
  name: string
  email: string
  phone: string
  organization_name: string
  city: string
  comment: string
}
export const emptyForm: FormValues = {
  name: '',
  email: '',
  phone: '',
  organization_name: '',
  city: '',
  comment: '',
}
const length = (value: string) => [...value].length
export function validateForm(
  v: FormValues,
  support = false,
): Partial<Record<keyof FormValues | 'channel', string>> {
  const errors: ReturnType<typeof validateForm> = {}
  if (!v.name.trim() || length(v.name) > 200)
    errors.name = 'Укажите имя: от 1 до 200 символов.'
  if (!v.email && !v.phone) errors.channel = 'Укажите email или телефон.'
  if (
    v.email &&
    (length(v.email) > 254 ||
      !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(v.email) ||
      (v.email.split('@')[0]?.length ?? 0) > 64)
  )
    errors.email = 'Проверьте email (до 254 символов).'
  const digits = v.phone.replace(/[^0-9]/g, '').length
  if (
    v.phone &&
    (length(v.phone) > 32 ||
      !/^\+?[0-9][0-9 ()-]*$/.test(v.phone) ||
      digits < 7 ||
      digits > 15)
  )
    errors.phone =
      'Телефон: 7–15 цифр, можно +, пробелы, скобки и дефисы; до 32 символов.'
  for (const key of ['organization_name', 'city'] as const)
    if (v[key] && (!v[key].trim() || length(v[key]) > 200))
      errors[key] = 'Введите от 1 до 200 символов или оставьте поле пустым.'
  if (length(v.comment) > 2000)
    errors.comment = 'Комментарий: не более 2000 символов.'
  if (support && !v.comment.trim())
    errors.comment = 'Опишите проблему: от 1 до 2000 символов.'
  return errors
}
