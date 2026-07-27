// Verify the Mermaid label-repair rescue pass (frontend/utils/mermaidRepair.ts)
// against the REAL Mermaid parser — the same engine DocMermaid.vue renders with.
//
// It proves the feedback loop: the diagram from the failing report parses only
// AFTER repair, valid diagrams are left untouched and still parse, and the
// transform is idempotent.
//
// Requires `mermaid` (^11) and `jsdom` on the module path. Easiest:
//   cd frontend && yarn install      # brings in mermaid; jsdom is transitive-free, add if missing
//   node --experimental-strip-types ../tools/agent/verify_mermaid_repair.mjs
// or run from any dir where `npm i mermaid jsdom` has been done.
import { JSDOM } from 'jsdom'
import { repairMermaid } from '../../frontend/utils/mermaidRepair.ts'

const dom = new JSDOM('<!doctype html><html><body></body></html>')
globalThis.window = dom.window
globalThis.document = dom.window.document

const mermaid = (await import('mermaid')).default
mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' })

async function parses(src) {
  try { await mermaid.parse(src); return true } catch { return false }
}

// The exact diagram from the failing Hebrew report (RTL + SUM(Invoice.Total)).
const REPORT = `flowchart TD
    A(["זיהוי<br/>כל יום בשעה 09:00<br/>Asia/Tel_Aviv"]) --> B[היום הקלנדרי הקודם<br/>הפעלת האוטומציה]
    B --> C[שליפת רשומות מטבלת<br/>Invoice לפי InvoiceDate]
    C --> D[סינון חשבוניות<br/>בטווח של היום הקודם בלבד]
    D --> E[חישוב סך ההכנסות<br/>SUM(Invoice.Total)]
    E --> F[ספירת מספר החשבוניות<br/>בטווח התאריך]
    F --> G[בניית הודעה קצרה בעברית<br/>כולל סכום ההכנסות וטווח התאריכים]
    G --> H[שליחת מייל למשתמש]
    H --> I([סיום])

    C -.מקור הנתונים.-> J[(Chinook Music Store<br/>טבלת Invoice)]
    J -.שדות בשימוש.-> K[InvoiceDate<br/>Total]`

// Cases the repair MUST rescue: raw fails, repaired parses.
const RESCUE = {
  'report diagram': REPORT,
  'rectangle + parens': 'flowchart TD\n D --> E[revenue SUM(Invoice.Total)]',
  'stadium + parens': 'flowchart TD\n A([total SUM(x)]) --> B',
  'cylinder + parens': 'flowchart TD\n A[(store SUM(x))] --> B',
  'circle + parens': 'flowchart TD\n A((count(n))) --> B',
  'br + parens together': 'flowchart TD\n A[rev<br/>SUM(x.y)] --> B',
}

// Cases the repair MUST NOT break: already valid, must still parse; idempotent.
const KEEP = {
  'plain labels': 'flowchart TD\n A[Start] --> B[End]',
  'already quoted parens': 'flowchart TD\n D --> E["revenue SUM(x)"]',
  'quoted stadium': 'flowchart LR\n A(["total SUM(x)"]) --> B',
  'br only, unquoted': 'flowchart TD\n A[line1<br/>line2] --> B',
  'edge pipe label': 'flowchart TD\n A -->|goes to| B',
  'sequence diagram (untouched)': 'sequenceDiagram\n Alice->>John: Hello John, how are you?',
}

let pass = true
const line = (ok, msg) => { console.log(`${ok ? 'ok  ' : 'FAIL'}  ${msg}`); if (!ok) pass = false }

console.log('— rescue: raw must FAIL, repaired must PARSE —')
for (const [name, src] of Object.entries(RESCUE)) {
  const rawOk = await parses(src)
  const fixed = repairMermaid(src)
  const fixedOk = await parses(fixed)
  line(!rawOk && fixedOk, `${name} (raw ${rawOk ? 'parsed?!' : 'failed'} → repaired ${fixedOk ? 'parses' : 'STILL FAILS'})`)
}

console.log('\n— keep: valid input stays valid, transform is idempotent —')
for (const [name, src] of Object.entries(KEEP)) {
  const fixed = repairMermaid(src)
  const fixedOk = await parses(fixed)
  const idempotent = repairMermaid(fixed) === fixed
  line(fixedOk && idempotent, `${name} (${fixedOk ? 'parses' : 'BROKEN'}, ${idempotent ? 'idempotent' : 'NOT idempotent'})`)
}

// Sequence diagram must be returned byte-for-byte unchanged (out of scope).
const seq = KEEP['sequence diagram (untouched)']
line(repairMermaid(seq) === seq, 'sequence diagram returned unchanged')

console.log('\n' + (pass ? 'PASS' : 'FAIL'))
process.exit(pass ? 0 : 1)
