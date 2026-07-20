import { defineNuxtConfig } from "nuxt/config"

export default defineNuxtConfig({
  devtools: { enabled: true },
  ssr: false,

  modules: [
    "@nuxt/ui",
    "@sidebase/nuxt-auth",
    'nuxt-tiptap-editor',
    '@nuxtjs/mdc',
    '@nuxt-alt/proxy',
    'nuxt-3-intercom',
    'nuxt-echarts',
    'nuxt-monaco-editor'
  ],

  echarts: {
    charts: [
      'BarChart',
      'LineChart',
      'PieChart',
      'ScatterChart',
      'EffectScatterChart',
      'BoxplotChart',
      'CandlestickChart',
      'GaugeChart',
      'FunnelChart',
      'HeatmapChart',
      'LinesChart',
      'MapChart',
      'ParallelChart',
      'RadarChart',
      'SunburstChart',
      'TreeChart',
      'TreemapChart'
    ],
    components: [
      'AriaComponent',
      'AxisPointerComponent',
      'BrushComponent',
      'CalendarComponent',
      'DataZoomComponent',
      'DataZoomInsideComponent',
      'DataZoomSliderComponent',
      'DatasetComponent',
      'GridComponent',
      'LegendComponent',
      'MarkLineComponent',
      'MarkPointComponent',
      'ParallelComponent',
      'RadarComponent'
    ]
  },

  intercom: {
    appId: 'ocwih86k',
    autoBoot: false
  },

  tiptap: {
    prefix: 'Tiptap'
  },

  plugins: [
    '~/plugins/vue-draggable-resizable.client.js',
    '~/plugins/vue-flow.client.js',
    '~/plugins/i18n.ts',
  ],

  css: [
    '~/assets/css/rtl.css',
    '~/assets/css/transitions.css',
    '~/assets/css/mobile.css',
  ],

  imports: {
    dirs: ['ee/composables'],
    presets: [
      { from: 'vue-i18n', imports: ['useI18n'] },
    ],
  },

  icon: {
    // Render icons as true inline <svg> instead of the default CSS mask-image
    // ('css') mode. CSS mode rasterizes the icon's mask at the element's
    // device-pixel resolution, so on Retina/HiDPI displays (DPR 2-3) icons look
    // crisp, but on DPR 1 monitors and fractional OS scaling (125%/150%) the
    // mask is sampled at too low a resolution and the heroicons (bug-ant,
    // thumbs up/down, etc.) appear pixelated. SVG mode lets the browser
    // rasterize the vector per-frame at the real composited resolution.
    mode: 'svg',
    localApiEndpoint: '/_nuxt_icon',
    serverBundle: {
      collections: ['heroicons'],
    },
    clientBundle: {
      scan: true,
    },
    fallbackToApi: false,
  },

  colorMode: {
    preference: 'system'
  },

  proxy: {
    debug: true,
    experimental: {
        listener: true
    },
    proxies: {
        '/ws/api': {
            target: 'ws://127.0.0.1:8000',
            ws: true,
            changeOrigin: true,
            secure: false,
            rewrite: (path) => path,
            headers: {
                'Upgrade': 'websocket',
                'Connection': 'Upgrade'
            }
        },
        '/.well-known': {
            target: 'http://127.0.0.1:8000',
            changeOrigin: true,
            secure: false,
            rewrite: (path) => path
        },
        '/mcp': {
            target: 'http://127.0.0.1:8000',
            changeOrigin: true,
            secure: false,
            rewrite: (path) => `/api${path}`
        },
        '/swagger': {
            target: 'http://127.0.0.1:8000',
            changeOrigin: true,
            secure: false,
            rewrite: (path) => path
        },
        '/openapi.json': {
            target: 'http://127.0.0.1:8000',
            changeOrigin: true,
            secure: false,
            rewrite: (path) => path
        },
        '/excel': {
            target: 'http://127.0.0.1:8000',
            changeOrigin: true,
            secure: false,
            rewrite: (path) => `/api${path}`
        },
        '/api': {
            target: 'http://127.0.0.1:8000',
            changeOrigin: true,
            secure: false,
            rewrite: (path) => path,
            headers: {
                'Connection': 'keep-alive'
            }
        }
    }
},

  auth: {
    baseURL: '/api/', // Proxy now handled by NGINX
    provider: {
      type: 'local',
      pages: {
        login: '/users/sign-in',
        signup: '/users/sign-up'
      },
      endpoints: {
        signIn: { path: '/auth/jwt/login', method: 'post' },
        signOut: { path: '/auth/jwt/logout', method: 'post' },
        signUp: { path: '/auth/jwt/register', method: 'post' },
        getSession: { path: '/users/whoami', method: 'get' }
      },
      token: {
        signInResponseTokenPointer: '/access_token',
        type: 'Bearer',
        maxAgeInSeconds: 60 * 60 * 24 * 7, // 7 days
        cookie: {
          name: 'auth_token',
          options: {
            path: '/',
            secure: process.env.NODE_ENV === 'production',
            sameSite: 'lax'
          }
        }
      },
      sessionDataType: { id: 'integer', name: 'string', email: 'string', is_superuser: 'boolean',
        organizations: '{ name: string, description: string | null, id: string, role: string, roles?: string[], permissions?: string[], resource_permissions?: Record<string, string[]>, is_enterprise?: boolean, usage_quota?: any }[]'
      },
    },
    session: {
      enableRefreshOnWindowFocus: true,
      enableRefreshPeriodically: false
    },
    globalAppMiddleware: {
      isEnabled: true
    },
    rewriteRedirects: true,
    fullPathRedirect: true
  },

  runtimeConfig: {
    public: {
      baseURL: '/api',
      wsURL: '/ws/api',
      environment: process.env.NODE_ENV,
    }
  },

  nitro: {
    experimental: {
      websocket: false
    }
  },

  // Allow ngrok domains to access the dev server (for Slack webhooks via frontend proxy)
  vite: {
    server: {
      allowedHosts: [
        '.ngrok-free.app'
      ]
    },
    optimizeDeps: {
      // nuxt-tiptap-editor puts the tiptap packages it registers into
      // build.transpile, which excludes them from Vite's dev pre-bundling —
      // they are served as raw ESM. The app's own tiptap deps
      // (@tiptap/extension-mention, @tiptap/suggestion) were NOT excluded, so
      // Vite pre-bundled them with a second, inlined copy of prosemirror-state.
      // Two prosemirror-state instances run separate auto-key counters, and
      // their unkeyed plugins collide ("RangeError: Adding different instances
      // of a keyed plugin (plugin$…)"), which aborts Editor creation and leaves
      // every instruction editor blank in dev. Exclude the whole
      // tiptap/prosemirror family so dev resolves exactly one copy of each
      // module. Production builds are unaffected (single Rollup graph).
      // CJS deps of excluded packages still need prebundling for ESM interop
      include: ['tiptap-markdown > markdown-it-task-lists'],
      exclude: [
        '@tiptap/extension-mention',
        '@tiptap/suggestion',
        '@tiptap/extension-table',
        '@tiptap/extension-table-row',
        '@tiptap/extension-table-cell',
        '@tiptap/extension-table-header',
        'tiptap-markdown',
        '@tiptap/pm',
        'prosemirror-changeset',
        'prosemirror-collab',
        'prosemirror-commands',
        'prosemirror-dropcursor',
        'prosemirror-gapcursor',
        'prosemirror-history',
        'prosemirror-inputrules',
        'prosemirror-keymap',
        'prosemirror-markdown',
        'prosemirror-menu',
        'prosemirror-model',
        'prosemirror-schema-basic',
        'prosemirror-schema-list',
        'prosemirror-state',
        'prosemirror-tables',
        'prosemirror-trailing-node',
        'prosemirror-transform',
        'prosemirror-view',
      ]
    }
  },

  routeRules: {
    '/data': { redirect: '/agents' },
    '/data/**': { redirect: '/agents/**' },
  },

  compatibilityDate: '2025-08-03',
})
