// Register the currency-`$` guard on every markstream-vue renderer (chat
// replies, shared conversations, notes, traces, eval runs). Each
// <MarkdownRender> builds its markdown-it instance in setup via getMarkdown(),
// which applies registered plugins, so registering here covers them all.
import { registerMarkdownPlugin, setDefaultMathOptions } from 'markstream-vue'
import { currencyDollarPlugin } from '~/utils/markdownMathDollars'

let registered = false

export default defineNuxtPlugin(() => {
  if (registered) return
  registered = true
  setDefaultMathOptions({ strictDelimiters: true })
  registerMarkdownPlugin(currencyDollarPlugin)
})
