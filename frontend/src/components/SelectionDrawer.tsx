import { useEffect, useRef, type ReactNode } from 'react'

/** Native modal supplies focus containment, Escape and inert background. */
export function SelectionDrawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current!
    const opener = document.activeElement
    dialog.showModal()
    dialog.querySelector('button')?.focus()
    const previous = document.documentElement.style.overflow
    document.documentElement.style.overflow = 'hidden'
    return () => {
      dialog.close()
      document.documentElement.style.overflow = previous
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])
  return <dialog ref={ref} className="selection-drawer" aria-label="Состав заявки" onCancel={e => { e.preventDefault(); onClose() }} onKeyDown={e => {
    if (e.key !== 'Tab') return
    const controls = [...ref.current!.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')].filter(el => el.getClientRects().length > 0)
    const first = controls[0], last = controls.at(-1)
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus() }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
  }} onClick={e => { if (e.target === ref.current) onClose() }}>
    <div className="drawer-content"><div className="dr-top"><h2>Ваша заявка</h2><button type="button" className="x" aria-label="Закрыть корзину" onClick={onClose}>×</button></div>
      {children}
      <button className="secondary-button" onClick={onClose}>Продолжить выбор</button>
    </div>
  </dialog>
}
