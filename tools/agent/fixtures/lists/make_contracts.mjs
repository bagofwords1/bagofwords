// Generate the synthetic contract PDFs used by the Agent Lists feedback loop
// (docs/feedback-loops/agent-lists.md). Rendered by headless Chromium so the
// Hebrew contract has a real RTL text layer.
//   node tools/agent/fixtures/lists/make_contracts.mjs   (needs `playwright`)
import { chromium } from 'playwright'
import { fileURLToPath } from 'url'
import path from 'path'

const here = path.dirname(fileURLToPath(import.meta.url))
const css = `body{font-family:Georgia,serif;margin:56px;line-height:1.55;font-size:13px;color:#111}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:22px 0 6px}.meta{color:#555;font-size:12px}`

const docs = {
  'msa_acme.pdf': `<h1>Master Services Agreement</h1><p class="meta">Agreement No. MSA-2025-014 · Effective 1 February 2025</p>
<p>This Master Services Agreement is entered into between <b>Northwind Analytics Inc.</b> ("Provider") and <b>Acme Logistics Ltd</b> ("Customer").</p>
<h2>1. Fees</h2><p>Customer shall pay Provider an annual fee of USD 120,000, excluding VAT, invoiced quarterly in advance.</p>
<h2>2. Term and Renewal</h2><p>The initial term ends on 31 January 2027. This Agreement renews automatically for successive one-year terms unless either party gives ninety (90) days written notice.</p>
<h2>3. Governing Law</h2><p>This Agreement is governed by the laws of the State of New York.</p>`,
  'globex_order_form.pdf': `<h1>Order Form</h1><p class="meta">Order GX-7731 · Signed 15 March 2024</p>
<p>Customer: <b>Globex GmbH</b>, Berlin. Supplier: Northwind Analytics Inc.</p>
<h2>Commercial Terms</h2><p>Subscription term: three (3) years commencing 1 January 2024. Total contract value: EUR 90,000 for the full three-year term, payable annually in equal instalments. Prices exclude VAT.</p>
<h2>Renewal</h2><p>The subscription expires on 31 December 2026 and does not renew automatically; any renewal requires a new signed Order Form.</p>`,
  'initech_sow.pdf': `<h1>Statement of Work</h1><p class="meta">SOW-INI-02 · Dated 10 June 2025</p>
<p>Client: <b>Initech Israel Ltd</b>. Contractor: Northwind Analytics Inc.</p>
<h2>Scope</h2><p>Data platform migration and twelve (12) months of managed support.</p>
<h2>Fees</h2><p>The fee for the twelve-month engagement is ILS 250,000, excluding VAT, payable in monthly instalments.</p>
<h2>Term</h2><p>This SOW is a fixed engagement that ends upon completion of the services. The parties have not agreed any extension mechanism.</p>`,
  'alpha_he.pdf': `<div dir="rtl" lang="he"><h1>הסכם שירותים</h1><p class="meta">הסכם מס׳ 2025-88 · בתוקף מיום 1 ביולי 2025</p>
<p>הסכם זה נערך בין <b>נורת׳ווינד אנליטיקס בע״מ</b> ("הספק") לבין <b>חברת אלפא בע״מ</b> ("הלקוח").</p>
<h2>1. תמורה</h2><p>הלקוח ישלם לספק דמי שירות שנתיים בסך 48,000 ש״ח, לא כולל מע״מ.</p>
<h2>2. תקופה וחידוש</h2><p>תקופת ההסכם מסתיימת ביום 30 ביוני 2027. ההסכם יתחדש באופן אוטומטי לתקופות נוספות של שנה, אלא אם מי מהצדדים הודיע אחרת בכתב.</p></div>`,
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || '/opt/pw-browsers/chromium' })
const page = await browser.newPage()
for (const [name, body] of Object.entries(docs)) {
  await page.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head><body>${body}</body></html>`)
  await page.pdf({ path: path.join(here, name), format: 'A4' })
  console.log('wrote', name)
}
await browser.close()
