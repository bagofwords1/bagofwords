import { VueFlow } from '@vue-flow/core'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.vueApp.component('VueFlow', VueFlow)
})
