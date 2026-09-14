import assert from 'node:assert/strict'

import {
  buildFileTree, folderAt, folderContents, searchFiles, fileName, formatBytes, fileExt, previewKind,
  isAnimatedImage,
} from '../../utils/fileTree.ts'

// --- the tree is rebuilt from paths ------------------------------------------
//
// File connections list files only, never folders. A SharePoint connection on
// every library ('*') prefixes each path with its library name, so libraries
// become the top-level folders.

const files = [
  { id: 'a', name: 'Budget.xlsx', path: 'Documents/Finance/Budget.xlsx' },
  { id: 'b', name: 'handbook.pdf', path: 'Policies/handbook.pdf' },
  { id: 'c', name: 'movies.xlsx', path: 'Documents/movies.xlsx' },
  { id: 'd', name: 'Q2.xlsx', path: 'Documents/Finance/2025/Q2.xlsx' },
  { id: 'e', name: 'Q10.xlsx', path: 'Documents/Finance/2025/Q10.xlsx' },
]
const root = buildFileTree(files)

assert.equal(root.fileCount, 5)
assert.deepEqual([...root.folders.keys()].sort(), ['Documents', 'Policies'])
assert.equal(root.files.length, 0)

const docs = folderAt(root, ['Documents'])
assert.equal(docs.fileCount, 4, 'a folder counts every file below it, not just its own')
assert.deepEqual(docs.files.map((f) => f.id), ['c'])

const finance = folderAt(root, ['Documents', 'Finance'])
assert.deepEqual(finance.segments, ['Documents', 'Finance'])
assert.equal(finance.fileCount, 3)

// A folder that vanished (the scope changed, then a refresh) is null, so the
// browser can fall back to the root instead of rendering a dead folder.
assert.equal(folderAt(root, ['Documents', 'Gone']), null)
assert.equal(folderAt(root, []), root)

// --- listing order ------------------------------------------------------------

const listing = folderContents(docs)
assert.deepEqual(listing.folders.map((f) => f.name), ['Finance'])
assert.deepEqual(listing.files.map((f) => f.id), ['c'])

// Numeric-aware: Q2 before Q10, the way a person reads them.
assert.deepEqual(
  folderContents(folderAt(root, ['Documents', 'Finance', '2025'])).files.map((f) => f.name),
  ['Q2.xlsx', 'Q10.xlsx'],
)

// --- odd path shapes -----------------------------------------------------------

// Documentum paths are absolute and Windows shares may use backslashes;
// neither may produce an empty-named or backslash-named folder.
const odd = buildFileTree([
  { id: '1', name: 'x.pdf', path: '/Finance/Reports/x.pdf' },
  { id: '2', name: 'y.docx', path: 'Legal\\Contracts\\y.docx' },
])
assert.deepEqual([...odd.folders.keys()].sort(), ['Finance', 'Legal'])
assert.equal(folderAt(odd, ['Legal', 'Contracts']).files[0].id, '2')

// No path at all (an older server) → every file sits at the root by name.
const flat = buildFileTree([{ id: 'opaque-1', name: 'Book 1.xlsx' }])
assert.equal(flat.files.length, 1)
assert.equal(fileName(flat.files[0]), 'Book 1.xlsx')

// The displayed name is the last path segment.
assert.equal(fileName({ id: 'z', path: 'Documents/Finance/Budget.xlsx' }), 'Budget.xlsx')

// --- search ---------------------------------------------------------------------

assert.deepEqual(searchFiles(files, ''), [])
assert.deepEqual(searchFiles(files, '   '), [])
assert.deepEqual(searchFiles(files, 'HANDBOOK').map((f) => f.id), ['b'])
// A folder name finds the files inside it, ordered by full path.
assert.deepEqual(searchFiles(files, 'finance').map((f) => f.id), ['d', 'e', 'a'])

// --- sizes ------------------------------------------------------------------------

assert.equal(formatBytes(null), '')
assert.equal(formatBytes(undefined), '')
assert.equal(formatBytes(0), '0 B')
assert.equal(formatBytes(512), '512 B')
assert.equal(formatBytes(1536), '1.5 KB')
assert.equal(formatBytes(20 * 1024 * 1024), '20 MB')

// --- preview kinds ----------------------------------------------------------------

assert.equal(previewKind('Report.PDF'), 'pdf')
assert.equal(previewKind('memo.docx'), 'office')
assert.equal(previewKind('deck.pptx'), 'office')
assert.equal(previewKind('Book 1.xlsx'), 'table')
assert.equal(previewKind('sales.csv'), 'table')
assert.equal(previewKind('photo.JPG'), 'image')
assert.equal(previewKind('notes.md'), 'text')
assert.equal(previewKind('סיכום רבעוני.txt'), 'text')
// Browsers can't draw TIFF; unknown and extensionless files have no preview.
assert.equal(previewKind('scan.tiff'), 'none')
assert.equal(previewKind('archive.zip'), 'none')
assert.equal(previewKind('Makefile'), 'none')
assert.equal(previewKind('.env'), 'none', 'a dotfile has no extension, just a name')
assert.equal(previewKind(null), 'none')
assert.equal(fileExt('a.tar.gz'), 'gz')

// --- animated images ----------------------------------------------------------------
//
// Synthetic containers: only chunk/header layout matters, never pixels or CRCs.

const bytes = (...parts) => {
  const arrs = parts.map((p) => (typeof p === 'string' ? [...p].map((c) => c.charCodeAt(0)) : p))
  return Uint8Array.from(arrs.flat())
}
const u32 = (n) => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]
const chunk = (type, len = 0) => [...u32(len), ...[...type].map((c) => c.charCodeAt(0)), ...new Array(len).fill(0), 0, 0, 0, 0]
const PNG_SIG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

const apng = bytes(PNG_SIG, chunk('IHDR', 13), chunk('acTL', 8), chunk('fcTL', 26), chunk('IDAT', 4), chunk('IEND'))
const png = bytes(PNG_SIG, chunk('IHDR', 13), chunk('IDAT', 4), chunk('IEND'))
// acTL after IDAT is not an APNG (the spec requires it first) — don't freeze it.
const lateActl = bytes(PNG_SIG, chunk('IHDR', 13), chunk('IDAT', 4), chunk('acTL', 8), chunk('IEND'))
assert.equal(isAnimatedImage(apng), true)
assert.equal(isAnimatedImage(png), false)
assert.equal(isAnimatedImage(lateActl), false)
// A truncated header must not throw or loop.
assert.equal(isAnimatedImage(bytes(PNG_SIG, u32(99999), 'IH')), false)

assert.equal(isAnimatedImage(bytes('GIF89a', new Array(20).fill(0), [0x21, 0xff, 0x0b], 'NETSCAPE2.0')), true)
assert.equal(isAnimatedImage(bytes('GIF89a', new Array(20).fill(0), [0x2c])), false)

const webp = (flags) => bytes('RIFF', u32(0), 'WEBP', 'VP8X', u32(10), [flags], new Array(9).fill(0))
assert.equal(isAnimatedImage(webp(0x02)), true)
assert.equal(isAnimatedImage(webp(0x10)), false, 'alpha flag only')
assert.equal(isAnimatedImage(bytes('RIFF', u32(0), 'WEBP', 'VP8 ', new Array(12).fill(0))), false)

assert.equal(isAnimatedImage(bytes([0xff, 0xd8, 0xff, 0xe0])), false, 'JPEG never animates')
assert.equal(isAnimatedImage(new Uint8Array()), false)

console.log('fileTree: ok')
