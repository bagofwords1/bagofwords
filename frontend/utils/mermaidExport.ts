// Mermaid → PNG download, shared by doc diagrams (DocMermaid) and chat
// diagrams (ChatMermaidBlock).
//
// The diagram is rendered again from source rather than rasterising the
// on-screen SVG because:
//  - Mermaid's default HTML labels live in <foreignObject>; drawing such an SVG
//    onto a canvas taints it (toBlob throws SecurityError), so the export
//    renders labels as plain SVG text;
//  - the export always uses the light theme on white, whatever the app theme.
// Mermaid is a singleton, so the app's usual config is restored afterwards.
// (DocMermaid and markstream both re-initialize before each render anyway.)

const BASE_CONFIG = {
  startOnLoad: false,
  suppressErrorRendering: true,
  securityLevel: 'strict' as const,
  fontFamily: 'ui-sans-serif, system-ui, sans-serif',
}

/** Render `code` with export-safe settings and trigger a PNG download. Throws on failure. */
export async function downloadMermaidPng(code: string, filename = 'diagram.png'): Promise<void> {
  const mermaid = (await import('mermaid')).default
  const isDark = document.documentElement.classList.contains('dark')
  try {
    mermaid.initialize({ ...BASE_CONFIG, theme: 'neutral', htmlLabels: false, flowchart: { htmlLabels: false } })
    const id = `mermaid-export-${Math.random().toString(36).slice(2)}`
    const { svg } = await mermaid.render(id, code)
    const blob = await svgToPngBlob(svg, 2)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } finally {
    mermaid.initialize({ ...BASE_CONFIG, theme: isDark ? 'dark' : 'neutral', htmlLabels: true, flowchart: { htmlLabels: true } })
  }
}

async function svgToPngBlob(svgText: string, scale: number): Promise<Blob> {
  const doc = new DOMParser().parseFromString(svgText, 'image/svg+xml')
  const el = doc.documentElement
  // Mermaid emits width="100%" plus a max-width style; size from the viewBox.
  const vb = (el.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number)
  const width = vb.length === 4 && vb[2] > 0 ? vb[2] : 800
  const height = vb.length === 4 && vb[3] > 0 ? vb[3] : 600
  el.setAttribute('width', String(width))
  el.setAttribute('height', String(height))
  el.removeAttribute('style')
  const xml = new XMLSerializer().serializeToString(el)
  const img = new Image()
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`
  await img.decode()
  const pad = 16
  const canvas = document.createElement('canvas')
  canvas.width = Math.ceil((width + pad * 2) * scale)
  canvas.height = Math.ceil((height + pad * 2) * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('no 2d context')
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.scale(scale, scale)
  ctx.drawImage(img, pad, pad, width, height)
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
  if (!blob) throw new Error('toBlob returned null')
  return blob
}
