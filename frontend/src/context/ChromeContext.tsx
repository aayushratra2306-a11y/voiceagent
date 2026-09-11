import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

export interface PageChrome {
  /** Shown beside the back arrow. Omit on the dashboard, which is the root. */
  title?: string
  /**
   * Where back goes when there is nowhere to go back TO — a bookmarked
   * link, a fresh tab, a page opened from an email. Normal back is history
   * based; this is only the floor.
   */
  backTo?: string
  /** Set false for pages that paint their own background (the call view). */
  blobs?: boolean
}

const DEFAULT: PageChrome = { blobs: true }

interface ChromeContextType {
  chrome: PageChrome
  setChrome: (c: PageChrome) => void
}

const ChromeContext = createContext<ChromeContextType | null>(null)

export function ChromeProvider({ children }: { children: ReactNode }) {
  const [chrome, setChrome] = useState<PageChrome>(DEFAULT)
  return (
    <ChromeContext.Provider value={{ chrome, setChrome }}>
      {children}
    </ChromeContext.Provider>
  )
}

export function useChrome() {
  const ctx = useContext(ChromeContext)
  if (!ctx) throw new Error('useChrome must be used inside ChromeProvider')
  return ctx
}

/**
 * How a page tells the shell what its header should say.
 *
 * Before this, six of seven pages hand-rolled their own header, and every
 * one of them hardcoded where its back arrow led. That is why the arrow
 * could lie: opening Approvals from a live call and pressing back went to
 * the dashboard rather than back to the call, because "back" was a fixed
 * destination rather than a step in the history.
 */
export function usePageChrome(title?: string, backTo?: string, blobs = true) {
  const { setChrome } = useChrome()
  useEffect(() => {
    setChrome({ title, backTo, blobs })
    // Reset on the way out so a page that sets nothing can't inherit the
    // previous page's title.
    return () => setChrome(DEFAULT)
    // Primitives only, so a page can pass a value that changes (a bot name
    // arriving from the API) and the header updates without looping.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title, backTo, blobs])
}
