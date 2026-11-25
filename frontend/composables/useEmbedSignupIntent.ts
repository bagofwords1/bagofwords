// The sign-in and sign-up forms' side of utils/embedSignupIntent.ts: submit
// one request with its host application's intent, or not at all.
import { sendWithEmbedIntent } from '~/utils/embedSignupIntent'

export function submitWithEmbedIntent<T>(
  redirect: unknown, send: (headers: Record<string, string>) => Promise<T>,
): Promise<T> {
  return sendWithEmbedIntent(redirect, url => $fetch<{ token?: string }>(url, { method: 'POST' }), send)
}
