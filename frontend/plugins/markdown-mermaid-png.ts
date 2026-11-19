// Every markstream-vue renderer (chat replies, shared conversations, notes)
// draws ```mermaid fences through ChatMermaidBlock, so its Export button
// downloads a PNG like doc diagrams do. Links go through ChatLink, which draws
// citation links (`#cite-…`, see composables/useCitations.ts) as source chips
// and hands every other link to the library's own renderer.
import { setCustomComponents } from 'markstream-vue'
import ChatMermaidBlock from '~/components/markdown/ChatMermaidBlock.vue'
import ChatLink from '~/components/markdown/ChatLink.vue'
import ChatTable from '~/components/markdown/ChatTable.vue'

let registered = false

export default defineNuxtPlugin(() => {
  if (registered) return
  registered = true
  setCustomComponents({ mermaid: ChatMermaidBlock, link: ChatLink, table: ChatTable })
})
