import assert from 'node:assert/strict'

import { PROJECT_COLORS, nextProjectColor } from '../../utils/projectColor.ts'

// --- accents assigned at creation --------------------------------------------
//
// Report rows show project membership as a thin rule in the project's color.
// A project created without one draws nothing, so a drag-and-drop into that
// folder looks like a no-op — which is why createProject() never sends a null
// color and this helper has to always return one.

// First project in an empty workspace takes the first swatch.
assert.equal(nextProjectColor([]), PROJECT_COLORS[0])
assert.equal(nextProjectColor(), PROJECT_COLORS[0])

// Used swatches are skipped, in palette order — not in the order projects
// happen to come back from the API.
assert.equal(nextProjectColor([{ color: PROJECT_COLORS[0] }]), PROJECT_COLORS[1])
assert.equal(nextProjectColor([{ color: PROJECT_COLORS[1] }]), PROJECT_COLORS[0])
assert.equal(
  nextProjectColor([{ color: PROJECT_COLORS[0] }, { color: PROJECT_COLORS[2] }]),
  PROJECT_COLORS[1],
)

// Pre-palette projects (color never set) don't consume a swatch.
assert.equal(nextProjectColor([{ color: null }, { color: undefined }, {}]), PROJECT_COLORS[0])

// Past the palette it wraps by count rather than giving up and returning null.
const full = PROJECT_COLORS.map((color) => ({ color }))
assert.equal(nextProjectColor(full), PROJECT_COLORS[0])
assert.equal(nextProjectColor([...full, { color: PROJECT_COLORS[0] }]), PROJECT_COLORS[1])

// Never empty — the caller sends this straight to the API as `color`.
for (let i = 0; i <= PROJECT_COLORS.length * 2; i++) {
  const existing = Array.from({ length: i }, (_, n) => ({ color: PROJECT_COLORS[n % PROJECT_COLORS.length] }))
  assert.ok(PROJECT_COLORS.includes(nextProjectColor(existing)))
}

console.log('new projects always get an accent color, unused swatches first')
