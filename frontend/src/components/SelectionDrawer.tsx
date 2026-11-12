import { useEffect, useRef, type ReactNode } from 'react'

/** Native modal supplies focus containment, Escape and inert background. */
export function SelectionDrawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current!
    const opener = document.activeElement
    dialog.showModal()
    const previous = document.documentElement.style.overflow
    document.documentElement.style.overflow = 'hidden'
    return () => {
      dialog.close()
      document.documentElement.style.overflow = previous
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])
  return <dialog ref={ref} className="selection-drawer" aria-label="Состав заявки" onCancel={onClose} onClick={e => { if (e.target === ref.current) onClose() }}>
    <div className="drawer-content"><div className="dr-top"><h2>Ваша заявка</h2><button type="button" className="x" aria-label="Закрыть корзину" onClick={onClose}>×</button></div>
      {children}
      <button className="secondary-button" onClick={onClose}>Продолжить выбор</button>
    </div>
  </dialog>
}
