// A sign-in or sign-up reached from an embedded artifact's sign-in window
// (redirect=/embed-signin?host=host_…) tells BOW so again at the moment the
// form is submitted, so the account it creates gets that host application's
// new-user role however long the person took to fill the form in (the intent
// is a short-lived cookie; see backend/app/core/workspace_access.py).
// A failure only means the account is created as an ordinary one.

const EMBED_SIGNIN = /^\/embed-signin\?(?:[^#]*&)?host=(host_[a-f0-9]{8,32})(?:&|$)/

export function embedHostOfRedirect(redirect: unknown): string | null {
  if (typeof redirect !== 'string') return null
  return EMBED_SIGNIN.exec(redirect)?.[1] || null
}

export async function renewEmbedSignupIntent(redirect: unknown): Promise<void> {
  const host = embedHostOfRedirect(redirect)
  if (!host) return
  try { await $fetch(`/api/embed/${host}/sso/intent`, { method: 'POST' }) } catch {}
}
