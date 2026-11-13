// MCP Apps host helpers (extension io.modelcontextprotocol/ui, ext-apps spec
// 2026-01-26). Pure functions only: the iframe and message plumbing live in
// components/tools/McpAppFrame.vue.

export const MCP_APPS_PROTOCOL_VERSION = '2026-01-26'

export type McpAppDisplayMode = 'inline' | 'fullscreen'

export interface JsonRpcMessage {
  jsonrpc: '2.0'
  id?: number | string
  method?: string
  params?: any
  result?: any
  error?: { code: number; message: string; data?: any }
}

export function isJsonRpc(msg: unknown): msg is JsonRpcMessage {
  return !!msg && typeof msg === 'object' && (msg as any).jsonrpc === '2.0'
}

export function isRequest(msg: JsonRpcMessage): boolean {
  return typeof msg.method === 'string' && msg.id !== undefined && msg.id !== null
}

export function isNotification(msg: JsonRpcMessage): boolean {
  return typeof msg.method === 'string' && (msg.id === undefined || msg.id === null)
}

// BOW's design tokens (assets/css/tokens.css) as the spec's style variables,
// so an app can match the chat it sits in. Read live, so they follow the theme.
const TOKEN_MAP: Record<string, string> = {
  '--color-background-primary': '--bow-bg-rgb',
  '--color-background-secondary': '--bow-bg-subtle-rgb',
  '--color-background-tertiary': '--bow-bg-muted-rgb',
  '--color-background-inverse': '--bow-bg-inverted-rgb',
  '--color-text-primary': '--bow-ink-rgb',
  '--color-text-secondary': '--bow-ink-muted-rgb',
  '--color-text-tertiary': '--bow-ink-subtle-rgb',
  '--color-text-inverse': '--bow-ink-inverted-rgb',
  '--color-border-primary': '--bow-line-rgb',
  '--color-border-secondary': '--bow-line-strong-rgb',
  '--color-border-tertiary': '--bow-line-subtle-rgb',
  '--color-ring-primary': '--bow-accent-rgb',
}

const RADIUS_MAP: Record<string, string> = {
  '--border-radius-sm': '--bow-radius-sm',
  '--border-radius-md': '--bow-radius-md',
  '--border-radius-lg': '--bow-radius-lg',
  '--border-radius-xl': '--bow-radius-xl',
}

export function hostStyleVariables(root: HTMLElement = document.documentElement): Record<string, string> {
  const cs = getComputedStyle(root)
  const out: Record<string, string> = {}
  for (const [key, token] of Object.entries(TOKEN_MAP)) {
    const triplet = cs.getPropertyValue(token).trim()
    if (triplet) out[key] = `rgb(${triplet})`
  }
  for (const [key, token] of Object.entries(RADIUS_MAP)) {
    const v = cs.getPropertyValue(token).trim()
    if (v) out[key] = v
  }
  const font = getComputedStyle(document.body || root).fontFamily
  if (font) out['--font-sans'] = font
  out['--font-mono'] = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
  return out
}

export interface HostContextInput {
  theme: 'light' | 'dark'
  locale: string
  displayMode: McpAppDisplayMode
  width?: number
  maxHeight?: number
  toolExecutionId?: string
  toolName?: string
}

export function buildHostContext(input: HostContextInput): Record<string, any> {
  let timeZone: string | undefined
  try { timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone } catch { timeZone = undefined }
  const touch = typeof window !== 'undefined' && 'ontouchstart' in window
  return {
    toolInfo: input.toolName ? { id: input.toolExecutionId, tool: { name: input.toolName } } : undefined,
    theme: input.theme,
    styles: { variables: hostStyleVariables() },
    displayMode: input.displayMode,
    availableDisplayModes: ['inline', 'fullscreen'],
    containerDimensions: {
      ...(input.width ? { width: Math.round(input.width) } : {}),
      maxHeight: input.maxHeight ?? 720,
    },
    locale: input.locale,
    timeZone,
    userAgent: typeof navigator !== 'undefined' ? navigator.userAgent : undefined,
    platform: 'web',
    deviceCapabilities: { touch, hover: !touch },
  }
}

export function hostCapabilities(canInteract: boolean): Record<string, any> {
  // What the app may ask of BOW. A read-only viewer (not the person who ran
  // the conversation) gets no server access and cannot message the model.
  const base: Record<string, any> = { openLinks: {}, logging: {} }
  if (!canInteract) return base
  return {
    ...base,
    serverTools: {},
    serverResources: {},
    message: { text: {} },
    updateModelContext: { text: {}, structuredContent: {} },
  }
}

// Only http(s) links leave the app; javascript:, data:, file: and the like
// are refused before anything is shown to the user.
export function safeExternalUrl(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw.length > 2048) return null
  try {
    const u = new URL(raw)
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.toString() : null
  } catch {
    return null
  }
}

// Text of a ui/message, as the chat would send it. Text blocks only (the
// host advertises message: { text: {} }), bounded so a runaway app cannot
// paste a novel into the conversation.
export function messageText(params: any, max = 4000): string {
  const content = params?.content
  const blocks = Array.isArray(content) ? content : content ? [content] : []
  const text = blocks
    .filter((b: any) => b && b.type === 'text' && typeof b.text === 'string')
    .map((b: any) => b.text)
    .join('\n')
    .trim()
  return text.length > max ? text.slice(0, max) : text
}

export function sandboxSrc(sandboxUrl: string, csp: Record<string, string[]> | undefined): string {
  const q = encodeURIComponent(JSON.stringify(csp || {}))
  return `${sandboxUrl}${sandboxUrl.includes('?') ? '&' : '?'}csp=${q}`
}

// A sandbox served from another origin may keep allow-same-origin (it is not
// BOW's origin). Served from BOW's own origin it must not: without the flag
// the frame gets an opaque origin and cannot reach BOW's cookies or storage.
export function sandboxAttr(sandboxUrl: string): string {
  let crossOrigin = false
  try {
    crossOrigin = new URL(sandboxUrl, window.location.href).origin !== window.location.origin
  } catch { crossOrigin = false }
  return crossOrigin ? 'allow-scripts allow-same-origin allow-forms' : 'allow-scripts allow-forms'
}

export function sandboxTargetOrigin(sandboxUrl: string): string {
  try {
    const origin = new URL(sandboxUrl, window.location.href).origin
    if (origin !== window.location.origin) return origin
  } catch { /* fall through */ }
  // Opaque-origin frame: its origin serializes as "null", which postMessage
  // cannot target; the frame is addressed by its window reference instead.
  return '*'
}
