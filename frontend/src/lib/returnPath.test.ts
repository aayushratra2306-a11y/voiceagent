import { describe, expect, it } from 'vitest'

import { safeReturnPath } from './returnPath'

describe('safeReturnPath', () => {
  it.each([
    ['/invite/tok_abc', 'the invitation page'],
    ['/o/org-1/dashboard', 'a normal in-app path'],
    ['/invite/tok?x=1#y', 'a path with a query and hash'],
  ])('allows %s (%s)', (good) => {
    expect(safeReturnPath(good)).toBe(good)
  })

  it.each([
    ['//evil.com', 'protocol-relative — goes off-origin'],
    ['https://evil.com', 'absolute'],
    ['http://evil.com', 'absolute, plain http'],
    ['javascript:alert(1)', 'javascript URI'],
    ['evil.com', 'bare host, no leading slash'],
    ['/\\evil.com', 'backslash some browsers read as a second slash'],
    ['', 'empty'],
  ])('refuses %s (%s)', (bad) => {
    expect(safeReturnPath(bad)).toBeNull()
  })

  it('refuses a missing value', () => {
    expect(safeReturnPath(null)).toBeNull()
    expect(safeReturnPath(undefined)).toBeNull()
  })
})
