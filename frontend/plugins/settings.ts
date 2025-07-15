export default defineNuxtPlugin(async (nuxtApp) => {
    const config = await $fetch('/api/settings')
    
    // Make settings available throughout the app
    nuxtApp.provide('settings', config)
    
    // Update runtime config with fetched settings
    const runtimeConfig = useRuntimeConfig()
    Object.assign(runtimeConfig.public, {
      googleSignIn: config.google_oauth?.enabled,
      deployment: config.deployment?.type,
      version: config.version,
      environment: config.environment,
      app_url: config.base_url,
      intercom: {
        enabled: config.intercom?.enabled
      }
    })
  })