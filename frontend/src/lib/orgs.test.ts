import { describe, it, expect } from 'vitest'
import { atLeast, ROLE_RANK } from './orgs'

describe('atLeast', () => {
  it('ranks the four roles in order', () => {
    expect(ROLE_RANK.viewer).toBeLessThan(ROLE_RANK.member)
    expect(ROLE_RANK.member).toBeLessThan(ROLE_RANK.admin)
    expect(ROLE_RANK.admin).toBeLessThan(ROLE_RANK.owner)
  })

  it('is true at the minimum and above it', () => {
    expect(atLeast('member', 'member')).toBe(true)
    expect(atLeast('owner', 'admin')).toBe(true)
  })

  it('is false below it, and false when the role is unknown', () => {
    expect(atLeast('viewer', 'member')).toBe(false)
    expect(atLeast(null, 'viewer')).toBe(false)
  })
})
