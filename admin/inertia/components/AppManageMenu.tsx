import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import StyledButton from '~/components/StyledButton'
import DynamicIcon, { DynamicIconName } from '~/components/DynamicIcon'

// One row in the overflow menu. `action` runs a callback and closes the menu;
// `toggle` is a labelled checkbox row that stays open so the flip is visible;
// `divider` separates the lifecycle/maintenance items from the destructive zone.
export type AppMenuItem =
  | {
      kind: 'action'
      icon: DynamicIconName
      label: string
      onClick: () => void
      disabled?: boolean
      // Renders the row in red (Delete, Wipe & reinstall) so the data-losing
      // actions read as dangerous and sit apart from the benign ones.
      danger?: boolean
    }
  | {
      kind: 'toggle'
      label: string
      checked: boolean
      onChange: (next: boolean) => void
      disabled?: boolean
    }
  | { kind: 'divider' }

export interface AppManageMenuProps {
  items: AppMenuItem[]
  // Disables the trigger while an install/operation is in flight.
  disabled?: boolean
}

// The single "⋯" overflow menu every Supply Depot row uses. A table row's
// actions column is too narrow to hold Open + Update + Stop/Start + Restart +
// the custom-app cluster + Wipe & reinstall on one line, so they wrap into a
// ragged three-high stack. Collapsing everything past Open (and a conditional
// Update) into this menu keeps each row on a single line. Used by BOTH surfaces
// — settings/apps.tsx (items open inline modals) and SupplyDepotCard.tsx (items
// call handlers.* props) — with identical behaviour; this is purely where the
// actions live, not what they do.
export default function AppManageMenu({ items, disabled }: AppManageMenuProps) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const menuId = useId()
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null)

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null)
      return
    }
    const place = () => {
      if (!containerRef.current || !panelRef.current) return
      const trigger = containerRef.current.getBoundingClientRect()
      const panel = panelRef.current.getBoundingClientRect()
      const gap = 8
      const top = trigger.bottom + gap + panel.height <= window.innerHeight
        ? trigger.bottom + gap
        : Math.max(gap, trigger.top - gap - panel.height)
      setPosition({
        left: Math.max(gap, Math.min(trigger.right - panel.width, window.innerWidth - panel.width - gap)),
        top,
      })
    }
    place()
    panelRef.current?.querySelector<HTMLElement>('button:not(:disabled), input:not(:disabled)')?.focus()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])

  // Click-outside + Escape close the panel. Listeners only attach while open.
  useEffect(() => {
    if (!open) return

    function onPointerDown(e: MouseEvent) {
      if (!containerRef.current?.contains(e.target as Node) && !panelRef.current?.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false)
        containerRef.current?.querySelector('button')?.focus()
      }
    }

    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  // Nothing to collapse (e.g. an installed app with unknown status and no custom
  // cluster) → render no trigger at all rather than an empty menu.
  if (items.length === 0) return null

  const itemBase =
    'flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50'

  return (
    <div ref={containerRef} className="relative inline-flex">
      <StyledButton
        variant="neutral"
        size="sm"
        className="h-9 w-9 shrink-0 p-0"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        aria-controls={open ? menuId : undefined}
      >
        <DynamicIcon icon="IconDots" className="h-4 w-4" />
      </StyledButton>
      {open && createPortal(
        <div
          id={menuId}
          ref={panelRef}
          role="menu"
          aria-label="App actions"
          style={{ position: 'fixed', left: position?.left ?? 0, top: position?.top ?? 0,
            visibility: position ? 'visible' : 'hidden', maxHeight: 'calc(100dvh - 16px)' }}
          className="z-[1000] w-60 max-w-[calc(100vw-16px)] overflow-y-auto rounded-lg border border-border-default bg-surface-elevated py-1 text-text-primary shadow-xl"
          onKeyDown={(event) => {
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
            event.preventDefault()
            const rows = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)') ?? [])
            const current = rows.indexOf(document.activeElement as HTMLElement)
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1
              : (current + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length
            rows[next]?.focus()
          }}
        >
          {items.map((item, i) => {
            if (item.kind === 'divider') {
              return (
                <div key={i} className="my-1 border-t border-desert-tan-lighter" aria-hidden />
              )
            }
            if (item.kind === 'toggle') {
              return (
                <label
                  key={i}
                  className="flex cursor-pointer select-none items-center justify-between gap-2 px-3 py-2 text-sm text-text-primary hover:bg-surface-secondary"
                >
                  {item.label}
                  <input
                    type="checkbox"
                    checked={item.checked}
                    onChange={(e) => item.onChange(e.target.checked)}
                    disabled={item.disabled}
                    className="accent-desert-orange h-4 w-4 rounded"
                  />
                </label>
              )
            }
            return (
              <button
                key={i}
                type="button"
                role="menuitem"
                className={`${itemBase} ${
                  item.danger
                    ? 'text-text-danger hover:bg-surface-secondary'
                    : 'text-text-primary hover:bg-surface-secondary'
                }`}
                onClick={() => {
                  setOpen(false)
                  item.onClick()
                }}
                disabled={item.disabled}
              >
                <DynamicIcon icon={item.icon} className="h-4 w-4 shrink-0" />
                {item.label}
              </button>
            )
          })}
        </div>, document.body
      )}
    </div>
  )
}
