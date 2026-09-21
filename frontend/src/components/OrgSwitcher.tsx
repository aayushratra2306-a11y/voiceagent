import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useOrg } from '../context/OrgContext'

/**
 * Which workspace am I in, and how do I get to another one.
 *
 * Switching is a navigation, not a state change: the organisation lives in
 * the address, so moving to another one means going to its dashboard. That
 * also means the browser's back button works, and two tabs can sit in two
 * organisations at once.
 */
export default function OrgSwitcher() {
  const { org, orgs } = useOrg()
  const [open, setOpen] = useState(false)
  const navigate = useNavigate()
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    function onEsc(e: KeyboardEvent) { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  return (
    <div className="relative" ref={box}>
      <button
        onClick={() => setOpen(o => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="text-xs px-2.5 py-1.5 rounded-lg text-slate-300 hover:bg-white/8 max-w-[180px] truncate"
      >
        {org.name}
        <span aria-hidden="true" className="ml-1.5 text-slate-600">▾</span>
      </button>

      {open && (
        <div role="menu" className="absolute left-0 mt-1 w-56 rounded-xl bg-[#11111f] border border-white/10 shadow-xl py-1 z-50">
          {orgs.map(o => (
            <button
              key={o.id}
              role="menuitem"
              onClick={() => { setOpen(false); navigate(`/o/${o.id}/dashboard`) }}
              className={`w-full text-left px-3 py-2 text-xs hover:bg-white/8 ${o.id === org.id ? 'text-cyan-300' : 'text-slate-300'}`}
            >
              <span className="block truncate">{o.name}</span>
              <span className="block text-[10px] text-slate-500">{o.role}</span>
            </button>
          ))}
          <div className="my-1 border-t border-white/8" />
          <button
            role="menuitem"
            onClick={() => { setOpen(false); navigate('/o') }}
            className="w-full text-left px-3 py-2 text-xs text-slate-400 hover:bg-white/8"
          >
            Create organisation
          </button>
        </div>
      )}
    </div>
  )
}
