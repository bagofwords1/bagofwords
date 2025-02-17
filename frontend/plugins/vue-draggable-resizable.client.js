// plugins/vue-draggable-resizable.client.js
import { defineNuxtPlugin } from '#app'
import VueDraggableResizable from 'vue-draggable-resizable'
import '~/plugins/vue-draggable-resizable.css'

export default defineNuxtPlugin(nuxtApp => {
  nuxtApp.vueApp.component('vue-draggable-resizable', VueDraggableResizable)
})
