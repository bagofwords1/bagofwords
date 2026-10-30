// The backend step a host application needs, per identity mode, in the
// languages customers' back offices run. Shown in Settings → Integrations →
// Host applications and in an artifact's Embed tab. Keep the two languages
// equivalent: same claims, same 60-second lifetime, same one-time jti.

export type ExampleLang = 'python' | 'node'
export const EXAMPLE_LANGS: { key: ExampleLang; label: string }[] = [
  { key: 'python', label: 'Python' },
  { key: 'node', label: 'Node.js' },
]

type Host = { name: string; public_id: string; identity_mode: string; identity_source?: string | null; issuer?: string | null }

const subjectOf = (host: Host) => {
  const src = host.identity_source || ''
  if (src.startsWith('member_attribute:')) return src.slice('member_attribute:'.length) || 'employee_id'
  if (src === 'ldap') return 'object_guid'
  if (src === 'scim') return 'scim_external_id'
  return 'id'
}

export function backendExample(host: Host, lang: ExampleLang): string {
  if (host.identity_mode === 'idp_token') {
    return lang === 'python'
      ? `# No backend step. The page passes the user's ID token from your identity
# provider (audience configured on "${host.name}"); BOW checks it with the
# provider's published keys.`
      : `// No backend step. The page passes the user's ID token from your identity
// provider (audience configured on "${host.name}"); BOW checks it with the
// provider's published keys.`
  }
  if (host.identity_mode === 'sso') {
    return lang === 'python'
      ? '# No backend step: people sign in to BOW once, in a small window.'
      : '// No backend step: people sign in to BOW once, in a small window.'
  }
  const sub = subjectOf(host)
  const issuer = host.issuer || '<issuer>'
  if (lang === 'python') {
    return `# pip install pyjwt[crypto]
# POST /bow/assertion: sign one short-lived assertion for the user YOUR app
# has signed in. Never take the user from the request body.
import os, time, uuid
import jwt

PRIVATE_KEY = os.environ["BOW_HOST_PRIVATE_KEY"]  # shown once in Settings → Integrations

def bow_assertion(user) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": str(user.${sub}),  # what "${host.name}" maps to a BOW member
            "iss": "${issuer}",
            "aud": "${host.public_id}",
            "iat": now,
            "exp": now + 60,
            "jti": uuid.uuid4().hex,
        },
        PRIVATE_KEY,
        algorithm="RS256",
    )

# Flask:
# @app.post("/bow/assertion")
# @login_required
# def assertion():
#     return {"assertion": bow_assertion(current_user)}`
  }
  return `// npm install jsonwebtoken
// POST /bow/assertion: sign one short-lived assertion for the user YOUR app
// has signed in. Never take the user from the request body.
import jwt from 'jsonwebtoken'
import crypto from 'node:crypto'

const PRIVATE_KEY = process.env.BOW_HOST_PRIVATE_KEY // shown once in Settings → Integrations

app.post('/bow/assertion', requireLogin, (req, res) => {
  const assertion = jwt.sign(
    { sub: String(req.user.${sub}) },          // what "${host.name}" maps to a BOW member
    PRIVATE_KEY,
    { algorithm: 'RS256', issuer: '${issuer}', audience: '${host.public_id}',
      expiresIn: 60, jwtid: crypto.randomUUID() },
  )
  res.json({ assertion })
})`
}
