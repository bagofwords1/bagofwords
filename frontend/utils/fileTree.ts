// Folder tree for the agent file browser.
//
// File connections list files only — no folder entries — each with a `path`
// relative to the connection's scope (library-prefixed when a SharePoint
// connection spans every library). The tree is rebuilt from those paths, so a
// folder shows up exactly when it holds an in-scope file: the include-globs
// are already applied by the listing, and nothing off-scope can leak in as an
// empty folder.

export interface BrowseFile {
  id: string
  name?: string | null
  path?: string | null
  size?: number | null
  modified_at?: string | null
  mime_type?: string | null
  web_url?: string | null
}

export interface FolderNode {
  name: string
  /** Segments from the root, e.g. ['Documents', 'Reports']; [] is the root. */
  segments: string[]
  folders: Map<string, FolderNode>
  files: BrowseFile[]
  /** Files in this folder and every folder below it. */
  fileCount: number
}

const newFolder = (name: string, segments: string[]): FolderNode => ({
  name, segments, folders: new Map(), files: [], fileCount: 0,
})

// Documentum paths start with '/', Windows shares may use '\' — neither
// should produce an empty or backslash-named folder. The id is a last resort
// only: Graph (SharePoint/OneDrive) ids are opaque ("01TP3T7WAPS6…"), not names.
//
// A path identical to the name is a root-level leaf, never split: that is what
// every file store sends for a top-level file, and what mail connectors send
// for every message — their "path" is the subject, and "Re: Q3 / Q4 forecast"
// must stay one item rather than become a folder "Re: Q3" holding "Q4 forecast".
export const pathSegments = (f: BrowseFile): string[] => {
  if (f.path && f.name && f.path === f.name) return [f.name]
  return String(f.path || f.name || f.id || '').replace(/\\/g, '/').split('/').filter(Boolean)
}

export const fileName = (f: BrowseFile): string => {
  const segs = pathSegments(f)
  return segs[segs.length - 1] || f.name || f.id
}

export function buildFileTree(files: BrowseFile[]): FolderNode {
  const root = newFolder('', [])
  for (const f of files) {
    const segs = pathSegments(f)
    let node = root
    node.fileCount++
    for (const seg of segs.slice(0, -1)) {
      let child = node.folders.get(seg)
      if (!child) {
        child = newFolder(seg, [...node.segments, seg])
        node.folders.set(seg, child)
      }
      child.fileCount++
      node = child
    }
    node.files.push(f)
  }
  return root
}

/** The folder at `segments`, or null when it no longer exists (e.g. after a refresh). */
export function folderAt(root: FolderNode, segments: string[]): FolderNode | null {
  let node: FolderNode | undefined = root
  for (const seg of segments) {
    node = node.folders.get(seg)
    if (!node) return null
  }
  return node
}

const byName = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })

/** Folders first, then files, each alphabetical. */
export function folderContents(node: FolderNode): { folders: FolderNode[]; files: BrowseFile[] } {
  return {
    folders: [...node.folders.values()].sort((a, b) => byName(a.name, b.name)),
    files: [...node.files].sort((a, b) => byName(fileName(a), fileName(b))),
  }
}

/** Case-insensitive match on the whole path, so a folder name finds its files too. */
export function searchFiles(files: BrowseFile[], query: string): BrowseFile[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  return files
    .filter((f) => pathSegments(f).join('/').toLowerCase().includes(q))
    .sort((a, b) => byName(pathSegments(a).join('/'), pathSegments(b).join('/')))
}

const TEXT_EXT = /\.(md|markdown|txt|csv|tsv|json|sql|ya?ml|log|xml|html?|ini|toml|env|sh)$/i

/** Heroicon for a file, from its mime type or, failing that, its extension. */
export function fileIcon(ct?: string | null, name?: string | null): string {
  const c = ct || ''
  const n = name || ''
  if (/^image\//.test(c) || /\.(png|jpe?g|gif|webp|svg)$/i.test(n)) return 'i-heroicons-photo'
  if (c === 'application/pdf' || /\.pdf$/i.test(n)) return 'i-heroicons-document'
  if (/csv|excel|spreadsheet/.test(c) || /\.(csv|tsv|xlsx?)$/i.test(n)) return 'i-heroicons-table-cells'
  if (/^text\/|json/.test(c) || TEXT_EXT.test(n)) return 'i-heroicons-document-text'
  return 'i-heroicons-paper-clip'
}

export type PreviewKind = 'pdf' | 'office' | 'image' | 'table' | 'text' | 'html' | 'none'

// Decided by extension, not the source's mime: providers are inconsistent (or
// silent) about mime, and the browser renders each kind from a blob it types
// itself, so a mislabelled file can never be rendered as something else.
// The mime is consulted only when there is NO extension (see previewKind).
const PREVIEW_BY_EXT: Record<string, PreviewKind> = {
  pdf: 'pdf',
  // Converted to PDF server-side (LibreOffice), then shown like a PDF.
  docx: 'office', doc: 'office', pptx: 'office', ppt: 'office', odt: 'office', odp: 'office', rtf: 'office',
  // No TIFF: browsers can't draw it.
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', bmp: 'image', svg: 'image',
  csv: 'table', tsv: 'table', xlsx: 'table', xls: 'table', xlsm: 'table',
  txt: 'text', md: 'text', markdown: 'text', json: 'text', jsonl: 'text', ndjson: 'text', log: 'text',
  xml: 'text', yaml: 'text', yml: 'text', sql: 'text', ini: 'text', toml: 'text',
  sh: 'text', py: 'text', js: 'text', ts: 'text', css: 'text',
  // Rendered in a sandboxed, script-less frame (see ConnectionFilePreview).
  html: 'html', htm: 'html',
}

// Extensionless entries only. OneNote pages are the case: titles carry no
// extension, but the connector declares text/html and serves the page HTML.
const PREVIEW_BY_MIME: Record<string, PreviewKind> = {
  'text/html': 'html',
  'text/plain': 'text',
  'application/pdf': 'pdf',
}

export const fileExt = (name?: string | null): string => {
  const n = String(name || '')
  const dot = n.lastIndexOf('.')
  return dot > 0 ? n.slice(dot + 1).toLowerCase() : ''
}

export const previewKind = (name?: string | null, mime?: string | null): PreviewKind => {
  const ext = fileExt(name)
  if (ext) return PREVIEW_BY_EXT[ext] || 'none'
  return PREVIEW_BY_MIME[String(mime || '').split(';')[0].trim().toLowerCase()] || 'none'
}

export const IMAGE_MIME_BY_EXT: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp', svg: 'image/svg+xml',
}

/**
 * True when an <img> would PLAY these bytes: an APNG, an animated GIF or an
 * animated WebP. Some sources hold multi-page documents as one animated image
 * (a 31-page SEC filing saved as an APNG, 10 frames a second) — SharePoint's
 * own viewer shows just the first frame, so the preview freezes these too.
 * Reads container headers only; never decodes pixels.
 */
export function isAnimatedImage(bytes: Uint8Array): boolean {
  const ascii = (o: number, n: number) => String.fromCharCode(...bytes.subarray(o, o + n))
  // PNG: an acTL chunk before the first IDAT is what makes it an APNG.
  if (bytes.length > 8 && bytes[0] === 0x89 && ascii(1, 3) === 'PNG') {
    let o = 8
    while (o + 8 <= bytes.length) {
      const len = ((bytes[o] << 24) | (bytes[o + 1] << 16) | (bytes[o + 2] << 8) | bytes[o + 3]) >>> 0
      const type = ascii(o + 4, 4)
      if (type === 'acTL') return true
      if (type === 'IDAT' || type === 'IEND') return false
      o += 12 + len // length + type + data + crc
    }
    return false
  }
  // GIF: animated GIFs carry the looping application extension, near the start.
  if (ascii(0, 4) === 'GIF8') {
    const head = ascii(0, Math.min(bytes.length, 4096))
    return head.includes('NETSCAPE2.0') || head.includes('ANIMEXTS1.0')
  }
  // WebP: the extended (VP8X) header's animation flag.
  if (ascii(0, 4) === 'RIFF' && ascii(8, 4) === 'WEBP' && ascii(12, 4) === 'VP8X') {
    return bytes.length > 20 && (bytes[20] & 0x02) !== 0
  }
  return false
}

export function formatBytes(size?: number | null): string {
  if (size == null || !Number.isFinite(Number(size))) return ''
  let n = Number(size)
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`
}
