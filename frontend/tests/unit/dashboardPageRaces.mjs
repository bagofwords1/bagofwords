import assert from 'node:assert/strict'
import { createRequire, stripTypeScriptTypes } from 'node:module'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { DEFAULT_STATE } from '../../utils/dashboardList.ts'

// The dashboards page's own script, compiled from its .vue file and run with
// the app's composables stubbed. The only thing played is the API (useMyFetch):
// each call waits until the test releases it, so a test decides what lands
// while what is in flight.

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const require = createRequire(join(frontend, 'package.json'))
const { parse, compileScript } = require('@vue/compiler-sfc')
const { computed, nextTick, reactive, ref, watch } = require('vue')

Object.assign(globalThis, { computed, nextTick, reactive, ref, watch })

const scratch = mkdtempSync(join(tmpdir(), 'dashboard-page-races-'))
process.on('exit', () => rmSync(scratch, { recursive: true, force: true }))

const utils = pathToFileURL(join(frontend, 'utils/dashboardList.ts')).href

// The .vue file's setup(), and its parsed template.
async function compiled(rel) {
  const filename = join(frontend, rel)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const { content } = compileScript(descriptor, { id: rel, inlineTemplate: false })
  const module = stripTypeScriptTypes(content)
    .replace(/import \{ defineComponent as _defineComponent \} from 'vue'/, 'const _defineComponent = c => c')
    .replace(/import (\w+) from '[^']+\.vue'/g, 'const $1 = null')
    .replace(/from '~\/utils\/dashboardList'/, `from '${utils}'`)
  const file = join(scratch, `${rel.replaceAll('/', '_')}.mjs`)
  writeFileSync(file, module)
  return { setup: (await import(pathToFileURL(file).href)).default.setup, template: descriptor.template.ast }
}

const flush = () => new Promise(done => setImmediate(done))

const page = await compiled('pages/dashboards/index.vue')

// --- the API ------------------------------------------------------------------------------

const CARDS = ['a', 'b']

function fakeApi() {
  const stars = new Set()
  const calls = []
  const answer = (data, error = null) => ({
    data: ref(data), error: ref(error), status: ref(error ? 'error' : 'success'),
  })
  const useMyFetch = (url, options = {}) => {
    const method = options.method
    if (url === '/artifacts/facets') {
      return Promise.resolve(answer({ tabs: { my: 2, shared: 0 }, new_count: 0, access: null, agents: [], owners: [] }))
    }
    return new Promise((done) => {
      // A list answers with what the server held when the request left.
      const held = CARDS.map(id => ({ id, is_starred: stars.has(id) }))
      calls.push({
        url,
        method,
        query: options.query,
        release(failure) {
          calls.splice(calls.indexOf(this), 1)
          if (failure) return done(answer(null, failure))
          const star = url.match(/^\/artifacts\/(\w+)\/star$/)
          if (star) method === 'POST' ? stars.add(star[1]) : stars.delete(star[1])
          done(answer(url === '/artifacts'
            ? { dashboards: held, meta: { total: 2, page: 1, limit: 15, total_pages: 1, has_next: false, has_prev: false } }
            : { previous_seen_at: null }))
        },
      })
    })
  }
  const lists = () => calls.filter(c => c.url === '/artifacts')
  const starCalls = () => calls.filter(c => c.url.endsWith('/star'))
  const seenCalls = () => calls.filter(c => c.url.endsWith('/seen'))
  return { useMyFetch, lists, starCalls, seenCalls, stars }
}

// --- the page, mounted on the My tab with its first list landed ----------------------------

async function openPage() {
  const api = fakeApi()
  const route = reactive({ path: '/dashboards', query: {} })
  const mounted = []
  const unmounting = []
  const toasts = []
  Object.assign(globalThis, {
    useMyFetch: api.useMyFetch,
    useI18n: () => ({ t: key => key }),
    useToast: () => ({ add: toast => toasts.push(toast) }),
    useErrorMessage: () => ({ getErrorMessage: () => 'failed' }),
    useRoute: () => route,
    useRouter: () => ({
      currentRoute: { value: route },
      push: (to) => { route.query = to.query },
      replace: (to) => { route.query = to.query },
    }),
    useFormatDate: () => ({ timeZone: ref(null) }),
    useExcel: () => ({ isExcel: ref(false) }),
    useHead: () => {},
    definePageMeta: () => {},
    onBeforeRouteLeave: () => {},
    onMounted: fn => mounted.push(fn),
    onBeforeUnmount: fn => unmounting.push(fn),
    navigateTo: () => {},
  })
  const vm = page.setup({}, { expose: () => {} })
  await mounted[0]()
  await flush()
  assert.equal(api.lists().length, 1, 'mounting asks for the list once')
  api.lists()[0].release()
  await flush()
  assert.equal(vm.isLoading.value, false)
  return {
    vm,
    api,
    route,
    toasts,
    card: id => vm.reports.value.find(c => c.id === id),
    unmount: () => unmounting.forEach(fn => fn()),
  }
}

// --- a star that lands during a list load -------------------------------------------------------

// The GET left before the star landed, so it answers with the old star. The
// page asks again once it lands, and the reader's star stands.
{
  const { vm, api, card } = await openPage()
  vm.load()
  await flush()
  assert.equal(api.lists().length, 1)
  vm.toggleStar(card('a'))
  await flush()
  api.starCalls()[0].release()
  await flush()
  assert.equal(api.lists().length, 1, 'no second list while the load is in flight: it would supersede it')

  api.lists()[0].release() // the old answer: not starred
  await flush()
  assert.equal(card('a').is_starred, true, 'the old answer does not flip a star that landed after it left')
  assert.equal(api.lists().length, 1, 'the star landed during the load: the page asks once more')
  assert.ok(api.lists()[0].query._fresh, 'its own GET, not one shared with an older call')
  assert.equal(vm.isLoading.value, false)

  api.lists()[0].release()
  await flush()
  assert.equal(card('a').is_starred, true, 'the star ends as the reader set it')
  assert.equal(api.lists().length, 0, 'the refetch finishing does not ask again')
}

// The same on Shared while the load still waits for the watermark: nothing has
// left yet, and the quiet refetch must not end the load's loading state.
{
  const { vm, api, route, card } = await openPage()
  route.query = { tab: 'shared' }
  await flush()
  assert.equal(api.seenCalls().length, 1)
  vm.toggleStar(card('a'))
  await flush()
  api.starCalls()[0].release()
  await flush()
  assert.equal(vm.isLoading.value, true, 'the load is still waiting for the watermark')
  assert.equal(api.lists().length, 0)

  api.seenCalls()[0].release()
  await flush()
  api.lists()[0].release()
  await flush()
  assert.equal(api.lists().length, 1, 'then one quiet refetch')
  api.lists()[0].release()
  await flush()
  assert.equal(api.lists().length, 0)
  assert.equal(vm.isLoading.value, false)
}

// A list still in flight when the Shared load starts waiting for the watermark:
// its old answer must not end the new load, or paint the old tab's cards as it.
{
  const { vm, api, route } = await openPage()
  vm.load()
  await flush()
  assert.equal(api.lists().length, 1)
  route.query = { tab: 'shared' }
  await flush()
  assert.equal(api.seenCalls().length, 1)
  const before = vm.reports.value
  api.lists()[0].release()
  await flush()
  assert.equal(vm.isLoading.value, true, 'the older answer does not end the Shared load')
  assert.equal(vm.reports.value, before, 'and is not shown')

  api.seenCalls()[0].release()
  await flush()
  api.lists()[0].release()
  await flush()
  assert.equal(vm.isLoading.value, false)
}

// A second star still in flight when the load lands: the refetch waits for it.
{
  const { vm, api, card } = await openPage()
  vm.load()
  await flush()
  vm.toggleStar(card('a'))
  vm.toggleStar(card('b'))
  await flush()
  api.starCalls()[0].release()
  await flush()
  api.lists()[0].release()
  await flush()
  assert.equal(api.lists().length, 0, 'one star is still in flight')
  assert.equal(card('b').is_starred, true, 'and it keeps its local state')

  api.starCalls()[0].release()
  await flush()
  assert.equal(api.lists().length, 1, 'the last star landed: one refetch')
  api.lists()[0].release()
  await flush()
  assert.equal(api.lists().length, 0)
  assert.equal(card('a').is_starred, true)
  assert.equal(card('b').is_starred, true)
}

// The page left while the load was in flight: nothing more is asked.
{
  const { vm, api, card, unmount } = await openPage()
  vm.load()
  await flush()
  vm.toggleStar(card('a'))
  await flush()
  api.starCalls()[0].release()
  await flush()
  unmount()
  api.lists()[0].release()
  await flush()
  assert.equal(api.lists().length, 0, 'no fetch after unmount')
}

// A star that failed changed nothing: it goes back, and nothing is refetched.
{
  const { vm, api, card, toasts } = await openPage()
  vm.load()
  await flush()
  vm.toggleStar(card('a'))
  await flush()
  api.starCalls()[0].release(new Error('refused'))
  await flush()
  assert.equal(card('a').is_starred, false)
  assert.equal(toasts.length, 1)
  api.lists()[0].release()
  await flush()
  assert.equal(api.lists().length, 0)
}

// The load itself failing still leads to the refetch; that one failing ends it,
// with no toast of its own.
{
  const { vm, api, card, toasts } = await openPage()
  vm.load()
  await flush()
  vm.toggleStar(card('a'))
  await flush()
  api.starCalls()[0].release()
  await flush()
  const logged = console.error
  console.error = () => {} // the page logs both failures
  api.lists()[0].release(new Error('down'))
  await flush()
  assert.equal(api.lists().length, 1)
  assert.equal(toasts.length, 1)
  api.lists()[0].release(new Error('still down'))
  await flush()
  console.error = logged
  assert.equal(api.lists().length, 0, 'a failed refetch does not ask again')
  assert.equal(toasts.length, 1)
}

// --- no star, or no load: as before ---------------------------------------------------------------

// A load with no star in it ends with its own GET.
{
  const { vm, api } = await openPage()
  vm.load()
  await flush()
  api.lists()[0].release()
  await flush()
  assert.equal(api.lists().length, 0)
}

// A star with nothing loading refetches at once.
{
  const { vm, api, card } = await openPage()
  vm.toggleStar(card('a'))
  await flush()
  api.starCalls()[0].release()
  await flush()
  assert.equal(api.lists().length, 1)
  assert.ok(api.lists()[0].query._fresh)
  api.lists()[0].release()
  await flush()
  assert.equal(card('a').is_starred, true)
  assert.equal(api.lists().length, 0)
}

// --- the funnel and the chips are Nuxt UI buttons --------------------------------------------------

const elements = (node, found = []) => {
  if (node.tag) found.push(node)
  for (const child of node.children ?? []) elements(child, found)
  return found
}
const binding = (el, name) => el.props.find(p => (p.arg?.content ?? p.name) === name)
const text = prop => prop?.value?.content ?? prop?.exp?.content ?? ''

const funnelSfc = await compiled('components/dashboards/DashboardFilterPopover.vue')

const funnel = elements(funnelSfc.template).find(el => text(binding(el, 'data-testid')) === 'dashboard-filters-button')
assert.equal(funnel?.tag, 'UButton')
for (const kept of ['aria-label', 'aria-expanded', 'aria-controls', 'ref']) assert.ok(binding(funnel, kept), kept)
assert.ok(elements(funnel).some(el => text(binding(el, 'data-testid')) === 'dashboard-filters-count'), 'the badge')

const pageElements = elements(page.template)
const accessChip = pageElements.find(el => text(binding(el, 'data-testid')).includes('dashboard-access-chip-'))
const typeChip = pageElements.find(el => el.props.some(p => p.name === 'for' && p.exp?.content.includes('typeFilters')))
for (const chip of [accessChip, typeChip]) {
  assert.equal(chip?.tag, 'UButton')
  assert.ok(binding(chip, 'aria-pressed'))
  assert.match(text(binding(chip, 'class')), /rounded-full/)
}
assert.ok(elements(accessChip).length > 1, 'the access chip keeps its count')

// Escape from inside the panel hands focus back to the funnel. A ref on a
// component is the instance, not the element: focus() is on its $el.
{
  const listeners = {}
  globalThis.document = {
    activeElement: {},
    addEventListener: (type, fn) => { listeners[type] = fn },
    removeEventListener: () => {},
  }
  const mounted = []
  Object.assign(globalThis, {
    useI18n: () => ({ t: key => key }),
    useId: () => 'panel',
    onMounted: fn => mounted.push(fn),
    onBeforeUnmount: () => {},
  })
  const vm = funnelSfc.setup({ state: DEFAULT_STATE, facets: null, tab: 'my' }, { expose: () => {}, emit: () => {} })
  mounted.forEach(fn => fn())
  let focused = 0
  vm.buttonRef.value = { $el: { focus: () => { focused += 1 } } }
  vm.panelRef.value = { contains: () => true }
  vm.open.value = true
  listeners.keydown({ key: 'Escape', defaultPrevented: false })
  assert.equal(vm.open.value, false)
  assert.equal(focused, 1)
}

console.log('dashboardPageRaces: ok')
