export default defineAppConfig({
  ui: {
    // The app uses blue as its accent throughout (buttons, links, active nav).
    // Nuxt UI defaults `primary` to green, which leaked into focus rings on
    // inputs/textareas that don't set an explicit color. Pin it to blue so all
    // form controls match the rest of the app.
    primary: 'blue',
  },
})
