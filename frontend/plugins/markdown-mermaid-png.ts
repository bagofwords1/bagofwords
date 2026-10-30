// Every markstream-vue renderer (chat replies, shared conversations, notes)
// draws ```mermaid fences through ChatMermaidBlock, so its Export button
// downloads a PNG like doc diagrams do.
import { setCustomComponents } from 'markstream-vue'
import ChatMermaidBlock from '~/components/markdown/ChatMermaidBlock.vue'

let registered = false

export default defineNuxtPlugin(() => {
  if (registered) return
  registered = true
  setCustomComponents({ mermaid: ChatMermaidBlock })
})
