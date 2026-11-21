// Every markstream-vue renderer (chat replies, shared conversations, notes)
// draws ```mermaid fences through ChatMermaidBlock, so its Export button
// downloads a PNG like doc diagrams do. Links go through ChatLink, which draws
// citation links (`#cite-…`, see composables/useCitations.ts) as source chips
// and hands every other link to the library's own renderer.
import { setCustomComponents } from 'markstream-vue'
import ChatMermaidBlock from '~/components/markdown/ChatMermaidBlock.vue'
import ChatLink from '~/components/markdown/ChatLink.vue'
import ChatTable from '~/components/markdown/ChatTable.vue'
import ChatFigure from '~/components/markdown/ChatFigure.vue'
import ChatFigurePaper from '~/components/markdown/ChatFigurePaper.vue'

let registered = false

export default defineNuxtPlugin(() => {
  if (registered) return
  registered = true
  // DESIGN MOCKUP: localStorage['bow.figureStyle'] = medium | paper | serif
  // swaps in the essay-style figure variants (ChatFigurePaper.vue).
  let paper = false
  try { paper = ['medium', 'paper', 'serif'].includes(localStorage.getItem('bow.figureStyle') || '') } catch { /* no storage */ }
  setCustomComponents({ mermaid: ChatMermaidBlock, link: ChatLink, table: ChatTable, 'bow-figure': paper ? ChatFigurePaper : ChatFigure })
})
