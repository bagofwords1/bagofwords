// Fictional English contracts for the Lists docs screenshots (docs.bagofwords.com).
// All companies and people are made up. Re-run to regenerate the PDFs:
//   node tools/agent/fixtures/lists_docs/make_docs_contracts.mjs [outDir]
// (needs `playwright`; uses the pre-installed Chromium).
import { chromium } from 'playwright'
import fs from 'fs'
import path from 'path'

const out = process.argv[2] || '/tmp/bow-docs-contracts'
fs.mkdirSync(out, { recursive: true })
const css = `body{font-family:Georgia,serif;margin:60px;line-height:1.6;font-size:13px;color:#111}
h1{font-size:21px;margin:0 0 4px}h2{font-size:14px;margin:22px 0 6px}.meta{color:#555;font-size:12px}`
const vendor = 'Northwind Analytics Inc.'

const docs = {
  'acme_logistics_msa.pdf': `<h1>Master Services Agreement</h1><p class="meta">Agreement No. MSA-2025-014 · Effective 1 February 2025</p>
<p>This Master Services Agreement is entered into between <b>${vendor}</b> ("Provider") and <b>Acme Logistics Ltd</b> ("Customer").</p>
<h2>1. Fees</h2><p>Customer shall pay Provider an annual fee of USD 120,000, excluding VAT, invoiced quarterly in advance.</p>
<h2>2. Term and Renewal</h2><p>The initial term ends on 31 January 2027. This Agreement renews automatically for successive one-year terms unless either party gives ninety (90) days written notice.</p>
<h2>3. Reporting</h2><p>Provider shall deliver a written service report to Customer within ten (10) business days after the end of each calendar quarter.</p>
<h2>4. Governing Law</h2><p>This Agreement is governed by the laws of the State of New York.</p>`,
  'globex_order_form.pdf': `<h1>Order Form</h1><p class="meta">Order GX-7731 · Signed 15 March 2024</p>
<p>Customer: <b>Globex GmbH</b>, Berlin. Supplier: ${vendor}</p>
<h2>Commercial Terms</h2><p>Subscription term: three (3) years commencing 1 January 2024. Annual subscription fee: EUR 30,000, payable annually in advance. Prices exclude VAT.</p>
<h2>Renewal</h2><p>The subscription expires on 31 December 2026 and does not renew automatically; any renewal requires a new signed Order Form.</p>
<h2>Governing Law</h2><p>This Order Form is governed by the laws of Germany.</p>`,
  'initech_sow.pdf': `<h1>Statement of Work</h1><p class="meta">SOW-INI-02 · Dated 10 June 2025</p>
<p>Client: <b>Initech Corp</b>. Contractor: ${vendor}</p>
<h2>Scope</h2><p>Data platform migration, to be completed by 30 September 2025, followed by twelve (12) months of managed support.</p>
<h2>Fees</h2><p>The fee for managed support is USD 8,500 per month, invoiced monthly in arrears.</p>
<h2>Term</h2><p>This SOW ends upon completion of the managed support period. Any extension is subject to mutual written agreement.</p>
<h2>Governing Law</h2><p>This SOW is governed by the Master Agreement between the parties dated 2 May 2022.</p>`,
  'blue_harbor_foods_msa.pdf': `<h1>Master Services Agreement</h1><p class="meta">Agreement No. MSA-2024-031 · Effective 1 September 2024</p>
<p>Between <b>${vendor}</b> ("Provider") and <b>Blue Harbor Foods Ltd</b> ("Customer").</p>
<h2>1. Fees</h2><p>Customer shall pay an annual platform fee of GBP 64,000, payable in two equal instalments on 1 September and 1 March.</p>
<h2>2. Term and Renewal</h2><p>The initial term is two (2) years and ends on 31 August 2026. Thereafter this Agreement renews automatically for one-year periods unless terminated by sixty (60) days written notice.</p>
<h2>3. Governing Law</h2><p>This Agreement is governed by the laws of England and Wales.</p>`,
  'pinecrest_health_order_form.pdf': `<h1>Order Form</h1><p class="meta">Order PH-0192 · Signed 3 April 2025</p>
<p>Customer: <b>Pinecrest Health Partners</b>. Supplier: ${vendor}</p>
<h2>Commercial Terms</h2><p>Annual fee: USD 45,000, invoiced annually in advance. Term: 1 May 2025 to 30 April 2026.</p>
<h2>Renewal</h2><p>This Order Form renews automatically for one additional year unless Customer notifies Supplier in writing at least thirty (30) days before the end of the term.</p>
<h2>Data Protection</h2><p>Supplier shall notify Customer of any personal data breach within seventy-two (72) hours of becoming aware of it.</p>
<h2>Governing Law</h2><p>Governed by the laws of the State of California.</p>`,
  'oakridge_capital_msa.pdf': `<h1>Master Services Agreement</h1><p class="meta">Agreement No. MSA-2025-022 · Effective 1 July 2025</p>
<p>Between <b>${vendor}</b> ("Provider") and <b>Oakridge Capital LLC</b> ("Customer").</p>
<h2>1. Fees</h2><p>Fees are set out in each Order Form issued under this Agreement. No Order Form has been executed as of the Effective Date.</p>
<h2>2. Term</h2><p>This Agreement remains in force until terminated by either party on ninety (90) days written notice.</p>
<h2>3. Governing Law</h2><p>This Agreement is governed by the laws of the State of Delaware.</p>`,
  'acme_logistics_amendment_1.pdf': `<h1>Amendment No. 1 to Master Services Agreement MSA-2025-014</h1><p class="meta">Dated 12 August 2025</p>
<p>Between <b>${vendor}</b> and <b>Acme Logistics Ltd</b>.</p>
<h2>1. Fees</h2><p>With effect from 1 September 2025, the annual fee in Section 1 of the Agreement is increased to USD 135,000, excluding VAT, to cover two additional warehouse sites.</p>
<h2>2. Other Terms</h2><p>All other terms of the Agreement remain unchanged.</p>`,
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || '/opt/pw-browsers/chromium' })
const page = await browser.newPage()
for (const [name, body] of Object.entries(docs)) {
  await page.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head><body>${body}</body></html>`)
  await page.pdf({ path: path.join(out, name), format: 'A4' })
  console.log('wrote', path.join(out, name))
}
await browser.close()
