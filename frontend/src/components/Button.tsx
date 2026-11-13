import type { ComponentProps } from 'react'

type ButtonProps = ComponentProps<'button'> & {
  variant?: 'primary' | 'secondary'
}

/** Native button semantics, including disabled and explicit submit opt-in. */
export function Button({
  variant = 'secondary',
  type = 'button',
  className = '',
  ...props
}: ButtonProps) {
  return (
    <button
      {...props}
      type={type}
      className={`ui-button ${variant}-button ${className}`.trim()}
    />
  )
}
