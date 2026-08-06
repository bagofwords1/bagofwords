#!/usr/bin/env node
/**
 * ui-audit role diff — compare two sweeps of the same routes taken as different
 * identities, and report which controls each role can see.
 *
 * Gating differences are the point of the comparison, but they are not all
 * equal. Admin-only controls are usually correct. A control visible to the
 * LOWER-privileged role and not to admin is nearly always a bug in the gate's
 * condition, so it is called out separately rather than left in a symmetric
 * diff for someone to notice.
 */
import fs from 'node:fs'
import path from 'node:path'

const HELP = `
ui-audit diff-roles — diff control inventories between two role sweeps.

Usage:
  node diff-roles.mjs <higher-privilege-dir> <lower-privilege-dir> [--json]

Both arguments are sweep.mjs --out directories. Order matters: put the more
privileged identity first, so "only in B" can be flagged as suspicious.

  node sweep.mjs --routes /agents --login --out audit/agents-admin
  BOW_EMAIL=member@example.com node sweep.mjs --routes /agents --login --out audit/agents-member
  node diff-roles.mjs audit/agents-admin audit/agents-member
`

const args = process.argv.slice(2)
if (args.includes('--help') || args.includes('-h') || args.length < 2) {
  console.log(HELP)
  process.exit(args.length < 2 ? 2 : 0)
}
const asJson = args.includes('--json')
const [dirA, dirB] = args.filter(a => !a.startsWith('--'))

const loadRoutes = dir => {
  const routesDir = path.join(dir, 'routes')
  if (!fs.existsSync(routesDir)) {
    console.error(`Not a sweep output directory (no routes/): ${dir}`)
    process.exit(1)
  }
  const out = new Map()
  for (const f of fs.readdirSync(routesDir).filter(f => f.endsWith('.json'))) {
    const r = JSON.parse(fs.readFileSync(path.join(routesDir, f), 'utf8'))
    out.set(r.route, r)
  }
  return out
}

// Identity for matching a control across two runs. Deliberately not the
// selector: headless-ui ids and DOM order shift between renders, and a control
// that merely moved is not a gating difference.
const key = c => `${c.tag}${c.role ? `[${c.role}]` : ''}:${(c.name || '').trim() || '(unnamed)'}`

const A = loadRoutes(dirA)
const B = loadRoutes(dirB)
const report = []

for (const route of [...new Set([...A.keys(), ...B.keys()])].sort()) {
  const a = A.get(route)
  const b = B.get(route)
  const entry = { route, onlyInA: [], onlyInB: [], reachable: { a: !!a, b: !!b } }

  if (!a || !b) {
    entry.note = `route only swept in ${a ? dirA : dirB}`
    report.push(entry)
    continue
  }

  // A route the lower-privileged identity gets bounced off is gating working at
  // the router level, not a missing control — report it as such and move on.
  const finalA = a.finalUrl && new URL(a.finalUrl).pathname
  const finalB = b.finalUrl && new URL(b.finalUrl).pathname
  if (finalA !== finalB) {
    entry.note = `redirect differs: ${dirA} -> ${finalA}, ${dirB} -> ${finalB}`
  }

  const visible = r => (r.controls || []).filter(c => c.visible)
  const mapA = new Map(visible(a).map(c => [key(c), c]))
  const mapB = new Map(visible(b).map(c => [key(c), c]))
  for (const [k, c] of mapA) if (!mapB.has(k)) entry.onlyInA.push({ key: k, selector: c.selector, destructive: c.destructive })
  for (const [k, c] of mapB) if (!mapA.has(k)) entry.onlyInB.push({ key: k, selector: c.selector, destructive: c.destructive })
  report.push(entry)
}

if (asJson) {
  console.log(JSON.stringify({ higher: dirA, lower: dirB, routes: report }, null, 2))
  process.exit(0)
}

console.log(`\nhigher privilege: ${dirA}\nlower privilege:  ${dirB}\n`)
let suspicious = 0
for (const e of report) {
  console.log(`── ${e.route}`)
  if (e.note) console.log(`   note: ${e.note}`)
  if (!e.onlyInA.length && !e.onlyInB.length && !e.note) {
    console.log('   identical visible controls')
  }
  if (e.onlyInA.length) {
    console.log(`   gated away from the lower role (${e.onlyInA.length}) — expected if the matrix says so:`)
    for (const c of e.onlyInA) console.log(`     - ${c.key}${c.destructive ? '  [destructive]' : ''}`)
  }
  if (e.onlyInB.length) {
    suspicious += e.onlyInB.length
    console.log(`   !! visible ONLY to the lower role (${e.onlyInB.length}) — check the gate condition:`)
    for (const c of e.onlyInB) console.log(`     - ${c.key}${c.destructive ? '  [destructive]' : ''}`)
  }
  console.log('')
}

console.log(`${suspicious} control(s) visible only to the lower-privileged role.`)
console.log('Controls present for BOTH roles still need clicking as the lower role:')
console.log('a control that renders and then 403s is the bug this diff cannot see.')
