// Credential-free fixture using the production renderer and report CSS.
// Run from frontend: node tests/reports/chat-spacing-fixture.mjs
// Open /chat-spacing-fixture.html and call checkChatSpacing() in DevTools.
import { readFileSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import MarkdownRender from 'markstream-vue'
import { parse, compileStyle } from '@vue/compiler-sfc'
import postcss from 'postcss'
import tailwind from 'tailwindcss'
import loadConfig from 'tailwindcss/loadConfig.js'
const config = loadConfig(process.cwd() + '/tailwind.config.ts')
config.content = [{raw: 'my-2 pl-1.5 list-disc list-decimal', extension: 'html'}]
const reportSource = process.argv[2]
  ? execFileSync('git', ['show', `${process.argv[2]}:frontend/pages/reports/[id]/index.vue`], {encoding: 'utf8'})
  : readFileSync('pages/reports/[id]/index.vue', 'utf8')
const { descriptor } = parse(reportSource)
const compiled = compileStyle({ source: descriptor.styles[0].content, filename: 'report.vue', id: 'data-v-spacing', scoped: true })
if (compiled.errors.length) throw compiled.errors[0]
const css = (await postcss([tailwind(config)]).process('@tailwind base;\n@tailwind utilities;\n' + compiled.code, { from: 'spacing.css' })).css
const texts = {
  en: 'Revenue grew steadily across the year. Repeat customers contributed most of the increase, while acquisition costs remained stable.\n\nCompare the regional results before changing the marketing budget.\n\n- Rock: $826.65\n- Latin: $382.14\n- Metal: $261.36\n\nKeep the complete reporting period in view when comparing these results.\n\n1. Review the regional totals.\n2. Compare retention.\n   - Existing customers\n   - New customers',
  he: 'ההכנסות צמחו לאורך השנה. לקוחות חוזרים תרמו לרוב העלייה, ועלויות רכישת הלקוחות נותרו יציבות.\n\nהצעד הבא הוא להשוות את התוצאות האזוריות לפני שינוי תקציב השיווק.\n\n- רוק: $826.65\n- לטיני: $382.14\n- מטאל: $261.36\n\nכדאי לבחון את תקופת הדיווח המלאה כשמשווים בין התוצאות.\n\n1. בדיקת ההכנסות באזור.\n2. השוואת שימור לקוחות.\n   - לקוחות קיימים\n   - לקוחות חדשים'
}
const rendererCSS = readFileSync('node_modules/markstream-vue/dist/index.css', 'utf8')
// SSR omits the renderer component scope attribute; preserve its marker styles.
const markerScope = rendererCSS.match(/\.list-item\[(data-v-[^\]]+)\]::marker/)[1]
let cards = ''
for (const [lang, content] of Object.entries(texts)) {
  let html = await renderToString(createSSRApp({ render: () => h(MarkdownRender, { content, final: true, typewriter: false, class: 'markdown-content' }) }))
  html = html.replaceAll('<li ', `<li ${markerScope} `)
  // The live direction observer stamps list containers after rendering.
  html = html.replace(/<(ul|ol) /g, `<$1 dir="${lang === 'he' ? 'rtl' : 'ltr'}" `)
  if (lang === 'he') html = html.replaceAll('dir="auto"', 'dir="rtl"')
  cards += `<section lang="${lang}" dir="${lang === 'he' ? 'rtl' : 'ltr'}"><h2>${lang === 'he' ? 'עברית' : 'English'}</h2><div class="chat-messages" data-v-spacing><div class="markdown-wrapper" data-v-spacing>${html}</div></div></section>`
}
const check = `window.checkChatSpacing = () => [...document.querySelectorAll('section')].map(section => {
  const root = section.querySelector('.markdown-content');
  const blocks = [...root.children].filter(e => e.classList.contains('node-slot'));
  const gap = blocks[1].getBoundingClientRect().top - blocks[0].getBoundingClientRect().bottom;
  const items = [...root.querySelector('ul').children];
  const pitch = items[1].getBoundingClientRect().top - items[0].getBoundingClientRect().top;
  const p = root.querySelector('p');
  const markersMatch = [...root.querySelectorAll('li')].every(li => getComputedStyle(li, '::marker').color === getComputedStyle(li).color);
  const result = {lang: section.lang, markersMatch, paragraphGap: gap, listPitch: pitch, font: getComputedStyle(p).fontSize, lineHeight: getComputedStyle(p).lineHeight};
  result.pass = markersMatch && gap >= 8 && gap <= 12 && pitch >= 26 && pitch <= 28.1 && result.font === '15px' && result.lineHeight === '24px';
  return result;
});`
writeFileSync('public/chat-spacing-fixture.html', `<!doctype html><html><meta charset="utf-8"><title>Chat spacing regression fixture</title><style>${readFileSync('node_modules/markstream-vue/dist/index.css','utf8')}\n${readFileSync('assets/css/tokens.css','utf8')}\n${readFileSync('assets/css/rtl.css','utf8')}\n${css}\nbody {padding:32px;background:#fff;color:#111827;font-family:-apple-system,system-ui,sans-serif} main {display:grid;grid-template-columns:1fr 1fr;gap:48px;max-width:1200px;margin:auto} section {min-width:0} section>h2 {font-size:14px;color:#64748b;margin-bottom:24px} .markdown-content {--bow-chat-font-size:15px;--bow-chat-line-height:24px} @media(max-width:700px){main{grid-template-columns:1fr}}</style><main>${cards}</main><script>${check}</script></html>`)
console.log('Fixture ready: /chat-spacing-fixture.html')
