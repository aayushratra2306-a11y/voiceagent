/**
 * Found live 2026-09-13 — creating an account showed "[object Object]".
 *
 * The password was five characters and the backend rightly refused it
 * (12 is the floor, review finding I6). What the user saw instead of why
 * was "[object Object]", because FastAPI reports a validation failure as a
 * LIST of objects:
 *
 *   {"detail": [{"type": "string_too_short", "loc": ["body", "password"],
 *                "msg": "String should have at least 12 characters",
 *                "input": "abcde", "ctx": {"min_length": 12}}]}
 *
 * and api.ts did `new Error(err.detail ?? ...)`. Error() stringifies its
 * argument, and an array of objects stringifies to exactly that text.
 *
 * Not a sign-up bug: the same line sat behind every request in the app, so
 * any form the backend validated failed the same unreadable way.
 *
 * The body above is the real one, captured from this project's own
 * RegisterRequest rather than written from memory — and it is why one test
 * below is about the PASSWORD. FastAPI echoes the submitted value back in
 * `input`. A fix that rendered the body, or JSON.stringify'd it to be
 * "safe", would print the user's password on the screen.
 */

import { describe, expect, it } from 'vitest'

import { errorMessage } from './api'

const shortPassword = {
  detail: [{
    type: 'string_too_short',
    loc: ['body', 'password'],
    msg: 'String should have at least 12 characters',
    input: 'abcde',
    ctx: { min_length: 12 },
  }],
}

const badEmailAndShortPassword = {
  detail: [
    {
      type: 'value_error',
      loc: ['body', 'email'],
      msg: 'value is not a valid email address: An email address must have an @-sign.',
      input: 'not-an-email',
      ctx: { reason: 'An email address must have an @-sign.' },
    },
    shortPassword.detail[0],
  ],
}

describe('turning a backend error into something a person can read', () => {
  it('never says [object Object] for a validation failure', () => {
    expect(errorMessage(shortPassword, 'Request failed')).not.toContain('[object')
  })

  it('says which field and what is wrong with it, in plain words', () => {
    expect(errorMessage(shortPassword, 'Request failed'))
      .toBe('Password must be at least 12 characters.')
  })

  it('reports every problem when there is more than one', () => {
    const message = errorMessage(badEmailAndShortPassword, 'Request failed')

    expect(message).toContain('Email')
    expect(message).toContain('@-sign')
    expect(message).toContain('Password must be at least 12 characters')
  })

  it('never shows the password the user typed', () => {
    // FastAPI echoes the submitted value in `input`. That is the user's
    // password, and it must not reach the screen.
    expect(errorMessage(shortPassword, 'Request failed')).not.toContain('abcde')
    expect(errorMessage(badEmailAndShortPassword, 'x')).not.toContain('abcde')
  })

  it('passes an ordinary string error through unchanged', () => {
    // HTTPException(detail="...") — every hand-written error in the backend.
    expect(errorMessage({ detail: 'Email already registered' }, 'Request failed'))
      .toBe('Email already registered')
  })

  it('falls back when there is nothing usable', () => {
    expect(errorMessage(null, 'Request failed')).toBe('Request failed')
    expect(errorMessage({}, 'Request failed')).toBe('Request failed')
    expect(errorMessage({ detail: [] }, 'Request failed')).toBe('Request failed')
    expect(errorMessage({ detail: { nothing: 'readable' } }, 'Upload failed')).toBe('Upload failed')
  })
})

describe('the request helper uses it', () => {
  it('a rejected sign-up surfaces the readable reason, not [object Object]', async () => {
    const { register } = await import('./api')
    const original = globalThis.fetch
    globalThis.fetch = (async () => ({
      ok: false,
      status: 422,
      statusText: 'Unprocessable Entity',
      json: async () => shortPassword,
    })) as unknown as typeof fetch

    try {
      await expect(register('a@b.co', 'abcde')).rejects.toThrow(
        'Password must be at least 12 characters.',
      )
    } finally {
      globalThis.fetch = original
    }
  })
})
