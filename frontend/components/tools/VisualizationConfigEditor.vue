<template>
  <div class="h-full flex flex-col text-xs text-gray-700">
    <!-- Scrollable content -->
    <div class="flex-1 overflow-y-auto overflow-x-hidden space-y-3 pr-1">
    <!-- Type selector -->
    <div>
      <div class="font-medium text-gray-800 mb-1">Type</div>
      <select v-model="local.type" class="w-full border rounded px-2 py-1.5 bg-white">
        <option v-for="opt in typeOptionsFromMeta" :key="opt" :value="opt">{{ opt }}</option>
      </select>
    </div>

    <!-- Encoding editor (dynamic by type) -->
    <div v-if="showEncoding">
      <div class="flex items-center justify-between">
        <div class="font-medium text-gray-800">Encoding</div>
        <button class="text-[11px] text-gray-500 hover:text-gray-700" @click="detectEncoding">Detect from data</button>
      </div>

      <!-- Bar/Line/Area -->
      <div v-if="isType(['bar_chart','line_chart','area_chart'])" class="space-y-2">
        <div>
          <div class="text-gray-600 mb-1">Category</div>
          <select v-model="encoding.category" class="w-full border rounded px-2 py-1 bg-white">
            <option value="">-- Select column --</option>
            <optgroup label="Suggested">
              <option v-for="c in stringColumns" :key="`cat-s-${c}`" :value="c">{{ c }}</option>
            </optgroup>
            <optgroup label="All columns">
              <option v-for="c in otherStringColumns" :key="`cat-a-${c}`" :value="c">{{ c }}</option>
            </optgroup>
          </select>
        </div>
        <div>
          <div class="text-gray-600 mb-1">Series</div>
          <div class="space-y-2">
            <div v-for="(s, idx) in encoding.series" :key="idx" class="flex items-center space-x-2">
              <input v-model="s.name" placeholder="name" class="flex-1 border rounded px-2 py-1" />
              <select v-model="s.value" class="w-40 border rounded px-2 py-1 bg-white">
                <option value="">value</option>
                <optgroup label="Suggested">
                  <option v-for="c in numericColumns" :key="`val-s-${c}`" :value="c">{{ c }}</option>
                </optgroup>
                <optgroup label="All columns">
                  <option v-for="c in otherNumericColumns" :key="`val-a-${c}`" :value="c">{{ c }}</option>
                </optgroup>
              </select>
              <button class="px-2 py-1 text-[11px] border rounded text-gray-600 hover:bg-gray-50" @click="removeSeries(idx)">Remove</button>
            </div>
          </div>
          <button class="mt-2 px-2 py-1 text-[11px] border rounded text-gray-600 hover:bg-gray-50" @click="addSeries">Add series</button>
        </div>
        
      </div>

      <!-- Pie -->
      <div v-else-if="isType(['pie_chart'])" class="space-y-2">
        <div>
          <div class="text-gray-600 mb-1">Category</div>
          <select v-model="encoding.category" class="w-full border rounded px-2 py-1 bg-white">
            <option value="">-- Select column --</option>
            <optgroup label="Suggested">
              <option v-for="c in stringColumns" :key="`pie-cat-s-${c}`" :value="c">{{ c }}</option>
            </optgroup>
            <optgroup label="All columns">
              <option v-for="c in otherStringColumns" :key="`pie-cat-a-${c}`" :value="c">{{ c }}</option>
            </optgroup>
          </select>
        </div>
        <div>
          <div class="text-gray-600 mb-1">Value</div>
          <select v-model="encoding.value" class="w-full border rounded px-2 py-1 bg-white">
            <option value="">-- Select column --</option>
            <optgroup label="Suggested">
              <option v-for="c in numericColumns" :key="`pie-val-s-${c}`" :value="c">{{ c }}</option>
            </optgroup>
            <optgroup label="All columns">
              <option v-for="c in otherNumericColumns" :key="`pie-val-a-${c}`" :value="c">{{ c }}</option>
            </optgroup>
          </select>
        </div>
      </div>

      <!-- Scatter -->
      <div v-else-if="isType(['scatter_plot'])" class="space-y-2">
        <div class="grid grid-cols-2 gap-2">
          <div>
            <div class="text-gray-600 mb-1">X</div>
            <select v-model="encoding.x" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`scat-x-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`scat-x-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Y</div>
            <select v-model="encoding.y" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`scat-y-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`scat-y-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
        </div>
      </div>

      <!-- Heatmap -->
      <div v-else-if="isType(['heatmap'])" class="space-y-2">
        <div class="grid grid-cols-3 gap-2">
          <div>
            <div class="text-gray-600 mb-1">X</div>
            <select v-model="encoding.x" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in stringColumns" :key="`heat-x-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherStringColumns" :key="`heat-x-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Y</div>
            <select v-model="encoding.y" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in stringColumns" :key="`heat-y-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherStringColumns" :key="`heat-y-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Value</div>
            <select v-model="encoding.value" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`heat-v-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`heat-v-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
        </div>
      </div>

      <!-- Candlestick -->
      <div v-else-if="isType(['candlestick'])" class="space-y-2">
        <div class="grid grid-cols-2 gap-2">
          <div>
            <div class="text-gray-600 mb-1">Time/Key</div>
            <select v-model="encoding.key" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in stringColumns" :key="`can-k-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherStringColumns" :key="`can-k-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Open</div>
            <select v-model="encoding.open" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`can-o-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`can-o-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Close</div>
            <select v-model="encoding.close" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`can-c-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`can-c-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Low</div>
            <select v-model="encoding.low" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`can-l-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`can-l-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">High</div>
            <select v-model="encoding.high" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`can-h-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`can-h-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
        </div>
      </div>

      <!-- Treemap -->
      <div v-else-if="isType(['treemap'])" class="space-y-2">
        <div class="grid grid-cols-2 gap-2">
          <div>
            <div class="text-gray-600 mb-1">Name</div>
            <select v-model="encoding.name" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in stringColumns" :key="`tree-n-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherStringColumns" :key="`tree-n-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Value</div>
            <select v-model="encoding.value" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in numericColumns" :key="`tree-v-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherNumericColumns" :key="`tree-v-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Parent Id (optional)</div>
            <select v-model="encoding.parentId" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in stringColumns" :key="`tree-p-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherStringColumns" :key="`tree-p-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <div class="text-gray-600 mb-1">Id (optional)</div>
            <select v-model="encoding.id" class="w-full border rounded px-2 py-1 bg-white">
              <option value="">-- Select column --</option>
              <optgroup label="Suggested">
                <option v-for="c in stringColumns" :key="`tree-i-s-${c}`" :value="c">{{ c }}</option>
              </optgroup>
              <optgroup label="All columns">
                <option v-for="c in otherStringColumns" :key="`tree-i-a-${c}`" :value="c">{{ c }}</option>
              </optgroup>
            </select>
          </div>
        </div>
      </div>

      <!-- Radar -->
      <div v-else-if="isType(['radar_chart'])" class="space-y-2">
        <div>
          <div class="text-gray-600 mb-1">Series name key (row label)</div>
          <select v-model="encoding.key" class="w-full border rounded px-2 py-1 bg-white">
            <option value="">-- Select column --</option>
            <optgroup label="Suggested">
              <option v-for="c in stringColumns" :key="`rad-k-s-${c}`" :value="c">{{ c }}</option>
            </optgroup>
            <optgroup label="All columns">
              <option v-for="c in otherStringColumns" :key="`rad-k-a-${c}`" :value="c">{{ c }}</option>
            </optgroup>
          </select>
        </div>
        <div>
          <div class="text-gray-600 mb-1">Dimensions</div>
          <div class="space-y-2">
            <div v-for="(d, idx) in dimensions" :key="idx" class="flex items-center space-x-2">
              <select v-model="dimensions[idx]" class="flex-1 border rounded px-2 py-1 bg-white">
                <option value="">-- Select column --</option>
                <optgroup label="Suggested">
                  <option v-for="c in numericColumns" :key="`rad-d-s-${c}`" :value="c">{{ c }}</option>
                </optgroup>
                <optgroup label="All columns">
                  <option v-for="c in otherNumericColumns" :key="`rad-d-a-${c}`" :value="c">{{ c }}</option>
                </optgroup>
              </select>
              <button class="px-2 py-1 text-[11px] border rounded text-gray-600 hover:bg-gray-50" @click="removeDimension(idx)">Remove</button>
            </div>
          </div>
          <button class="mt-2 px-2 py-1 text-[11px] border rounded text-gray-600 hover:bg-gray-50" @click="addDimension">Add dimension</button>
        </div>
      </div>
    </div>

    <!-- Styling -->
    <div>
      <div class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2" @click="expanded.style = !expanded.style">
        <Icon :name="expanded.style ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Style
      </div>
      <Transition name="fade">
        <div v-if="expanded.style" class="space-y-3">
          <!-- Visibility toggles -->
          <div class="space-y-2">
            <!-- Legend - show for chart types that support it -->
            <label v-if="isType(['bar_chart','line_chart','area_chart','pie_chart','radar_chart'])" class="flex items-center space-x-2 text-xs">
              <input type="checkbox" v-model="local.legendVisible" class="rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
              <span class="text-gray-600">Show Legend</span>
            </label>
            
            <div v-if="isType(['bar_chart','line_chart','area_chart','scatter_plot','heatmap'])" class="space-y-2">
              <label class="flex items-center space-x-2 text-xs">
                <input type="checkbox" v-model="local.xAxisVisible" class="rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                <span class="text-gray-600">Show X Axis</span>
              </label>
              
              <!-- X-Axis Labels section - auto-expanded when X-axis is visible -->
              <div v-if="local.xAxisVisible && isType(['bar_chart','line_chart','area_chart','scatter_plot','heatmap'])" class="ml-6 mt-2">
                <div class="flex items-center cursor-pointer text-[10px] uppercase tracking-wide text-gray-500 mb-2" @click="expanded.xAxisLabels = !expanded.xAxisLabels">
                  <Icon :name="expanded.xAxisLabels ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
                  X-Axis Labels
                </div>
                <Transition name="fade">
                  <div v-if="expanded.xAxisLabels" class="grid grid-cols-2 gap-2 pl-4">
                    <div>
                      <div class="text-gray-600 mb-1 text-[10px]">Label rotation</div>
                      <select v-model.number="local.xAxisRotate" class="w-full border rounded px-2 py-1 bg-white text-[10px]">
                        <option :value="null">Auto</option>
                        <option :value="0">0° (horizontal)</option>
                        <option :value="45">45° (diagonal)</option>
                        <option :value="90">90° (vertical)</option>
                        <option :value="-45">-45° (diagonal)</option>
                      </select>
                    </div>
                    <div>
                      <div class="text-gray-600 mb-1 text-[10px]">Label interval</div>
                      <select v-model.number="local.xAxisInterval" class="w-full border rounded px-2 py-1 bg-white text-[10px]">
                        <option :value="null">Auto</option>
                        <option :value="0">Show all (0)</option>
                        <option :value="1">Every 2nd (1)</option>
                        <option :value="2">Every 3rd (2)</option>
                        <option :value="3">Every 4th (3)</option>
                      </select>
                    </div>
                  </div>
                </Transition>
              </div>
              
              <label class="flex items-center space-x-2 text-xs">
                <input type="checkbox" v-model="local.yAxisVisible" class="rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                <span class="text-gray-600">Show Y Axis</span>
              </label>
              
              <!-- Y-Axis section - placeholder for future controls -->
              <div v-if="local.yAxisVisible && isType(['bar_chart','line_chart','area_chart','scatter_plot','heatmap'])" class="ml-6 mt-2">
                <div class="flex items-center cursor-pointer text-[10px] uppercase tracking-wide text-gray-500 mb-2" @click="expanded.yAxisLabels = !expanded.yAxisLabels">
                  <Icon :name="expanded.yAxisLabels ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
                  Y-Axis Labels
                </div>
                <Transition name="fade">
                  <div v-if="expanded.yAxisLabels" class="text-[10px] text-gray-500 pl-4">
                    Y-axis controls will be available here in future updates.
                  </div>
                </Transition>
              </div>
            </div>
            
            <label v-if="isType(['bar_chart','line_chart','area_chart'])" class="flex items-center space-x-2 text-xs">
              <input type="checkbox" v-model="local.showGrid" class="rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
              <span class="text-gray-600">Show Grid lines</span>
            </label>
          </div>
        </div>
      </Transition>
    </div>
    </div>

    <!-- Sticky Actions -->
    <div class="flex-shrink-0 pt-3 mt-3 border-t bg-white flex items-center justify-end space-x-2">
      <div v-if="error" class="text-red-600 text-[11px] mr-auto">{{ error }}</div>
      <button class="px-2 py-1 text-[11px] border rounded text-gray-700 hover:bg-gray-50" @click="reset">Reset</button>
      <button class="px-2 py-1 text-[11px] border rounded text-gray-700 hover:bg-gray-50" @click="apply">Apply</button>
      <button class="px-3 py-1.5 text-[11px] rounded bg-gray-800 text-white hover:bg-gray-700 disabled:opacity-50" :disabled="saving" @click="save">
        <span v-if="saving">Saving…</span>
        <span v-else>Save</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch, onMounted } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'

interface Props {
  viz: any
  step?: any
}

const props = defineProps<Props>()
const emit = defineEmits(['apply', 'saved'])

const typeOptions = [
  'table',
  'bar_chart',
  'line_chart',
  'area_chart',
  'pie_chart',
  'scatter_plot',
  'heatmap',
  'candlestick',
  'treemap',
  'radar_chart',
  'count'
]

// Backend-provided capabilities per visualization type
const meta = ref<Record<string, any>>({})
const typeOptionsFromMeta = computed<string[]>(() => {
  // Check if meta has a 'types' or 'components' list, or look for capabilities per type
  const m = meta.value || {}
  if (Array.isArray(m.types)) return m.types
  if (Array.isArray(m.components)) return m.components
  // If capabilities is an object with type keys, use those
  if (m.capabilities && typeof m.capabilities === 'object') {
    const capKeys = Object.keys(m.capabilities)
    if (capKeys.length && capKeys.every(k => typeOptions.includes(k))) return capKeys
  }
  // Fallback to hardcoded type options
  return typeOptions
})
const capsForType = computed<Record<string, any>>(() => {
  const t = String(local.type || '').toLowerCase()
  const m = meta.value || {}
  // Try nested capabilities first, then direct type key
  const caps = m.capabilities?.[t] || m[t]
  return caps || { axes: false, legend: false, grid: false, labels: true, encodings: [] }
})

const saving = ref(false)
const error = ref('')

// UI section expand/collapse state
const expanded = reactive<{ typeData: boolean; style: boolean; xAxisLabels: boolean; yAxisLabels: boolean }>({ typeData: true, style: true, xAxisLabels: false, yAxisLabels: false })

function deepClone<T>(v: T): T { return JSON.parse(JSON.stringify(v || {})) }

// Helper to extract v2 or legacy values
function initFromView(view: any, step: any) {
  const v = view || {}
  const dm = step?.data_model || {}
  
  // Build encoding from view.encoding, or construct from data_model.series/view fields
  let enc = deepClone(v.encoding || {})
  if (!enc.category && !enc.series?.length && !enc.x && !enc.y) {
    // Try to build encoding from v2 fields or data_model
    if (v.x) enc.category = v.x
    if (v.y) {
      const yVals = Array.isArray(v.y) ? v.y : [v.y]
      enc.series = yVals.map((val: string, i: number) => ({ name: `Series ${i + 1}`, value: val }))
    }
    if (v.category) enc.category = v.category
    if (v.value) enc.value = v.value
    // Fallback to data_model.series
    if ((!enc.category || !enc.series?.length) && dm.series?.length) {
      const s0 = dm.series[0]
      enc.category = enc.category || s0.key
      enc.series = enc.series?.length ? enc.series : dm.series.map((s: any) => ({ name: s.name, value: s.value }))
      enc.value = enc.value || s0.value
      enc.x = enc.x || s0.x
      enc.y = enc.y || s0.y
    }
  }
  
  return {
    type: v.type || dm.type || 'table',
    // Encoding for UI binding
    encoding: enc,
    // v2 axis options (read from nested or flat legacy)
    xAxisVisible: v.axisX?.show ?? v.xAxisVisible ?? true,
    xAxisRotate: v.axisX?.rotate ?? v.xAxisLabelRotate ?? 45,
    xAxisInterval: v.axisX?.interval ?? v.xAxisLabelInterval ?? 0,
    yAxisVisible: v.axisY?.show ?? v.yAxisVisible ?? true,
    yAxisRotate: v.axisY?.rotate ?? 0,
    yAxisInterval: v.axisY?.interval ?? 0,
    // v2 legend - default to false (hidden by default)
    legendVisible: v.legend?.show ?? v.legendVisible ?? false,
    legendPosition: v.legend?.position ?? 'bottom',
    // v2 palette
    paletteTheme: v.palette?.theme ?? 'default',
    paletteScale: v.palette?.scale ?? 'primary',
    // v2 chart options
    stacked: v.stacked ?? false,
    smooth: v.smooth ?? (v.type === 'line_chart' || v.type === 'area_chart'),
    showGrid: v.showGrid ?? v.showGridLines ?? true,
    showDataZoom: v.showDataZoom ?? false,
    // Bar chart specific
    horizontal: v.horizontal ?? false,
    // Pie chart specific
    donut: v.donut ?? false,
    // Heatmap specific
    colorScheme: v.colorScheme ?? 'blue',
    showValues: v.showValues ?? true,
    // Legacy fields
    variant: v.variant || null,
    style: deepClone(v.style || {}),
    options: deepClone(v.options || {}),
  }
}

// Initialize local state - but keep encoding separate for reactivity
const initialState = initFromView(props.viz?.view, props.step)
const { encoding: initialEncoding, ...restInitial } = initialState

const local = reactive<any>({
  ...restInitial,
  encoding: {} // placeholder, will use separate reactive
})

// Encoding is a separate reactive for proper v-model binding in template
const encoding = reactive<any>(initialEncoding || {})
const dimensions = computed<string[]>({
  get: () => Array.isArray(encoding.dimensions) ? encoding.dimensions : (encoding.dimensions = []),
  set: (v: string[]) => { encoding.dimensions = v }
})

const isAreaVariant = computed<boolean>({
  get: () => local.variant === 'area',
  set: (v: boolean) => { local.variant = v ? 'area' : (local.variant === 'area' ? null : local.variant) }
})
const isSmoothVariant = computed<boolean>({
  get: () => local.variant === 'smooth',
  set: (v: boolean) => { local.variant = v ? 'smooth' : (local.variant === 'smooth' ? null : local.variant) }
})

const showEncoding = computed(() => !isType(['table','count']))
function isType(types: string[]): boolean { return types.includes(local.type) }

const allColumns = computed<string[]>(() => {
  const cols = props.step?.data?.columns || []
  const names = cols.map((c: any) => c.field || c.headerName || c.colId).filter(Boolean)
  return Array.from(new Set(names))
})
const numericColumns = computed<string[]>(() => allColumns.value.filter(isProbablyNumeric))
const stringColumns = computed<string[]>(() => allColumns.value.filter(c => !numericColumns.value.includes(c)))
const otherStringColumns = computed<string[]>(() => allColumns.value.filter(c => !stringColumns.value.includes(c)))
const otherNumericColumns = computed<string[]>(() => allColumns.value.filter(c => !numericColumns.value.includes(c)))

function isProbablyNumeric(name: string): boolean {
  // Heuristic: prefer columns with numeric-like values in first row if available
  try {
    const rows = props.step?.data?.rows || []
    if (!rows.length) return false
    const v = rows[0]?.[name]
    return typeof v === 'number' || (!!v && !Number.isNaN(Number(v)))
  } catch { return false }
}

function scoreValueColumn(name: string, category: string, indexInAll: number): number {
  const n = String(name || '').toLowerCase()
  const cat = String(category || '').toLowerCase()
  // Hard penalties
  if (!n) return -1_000_000
  if (n === cat) return -100_000
  if (/(^id$|_id$|id$)/i.test(n)) return -50_000
  // Positive signals for measure-like fields
  const positiveHints = ['revenue','amount','total','sum','count','price','value','sales','metric','measure','qty','quantity']
  let score = 0
  positiveHints.forEach((h, i) => { if (n.includes(h)) score += 100 - i })
  // Mild preference for earlier columns
  score += Math.max(0, 50 - indexInAll)
  return score
}

function pickBestNumericValue(category: string, numericCols: string[], allCols: string[]): string | undefined {
  if (!Array.isArray(numericCols) || !numericCols.length) return undefined
  let best: { name: string; score: number } | null = null
  numericCols.forEach((col) => {
    const idx = Math.max(0, allCols.indexOf(col))
    const s = scoreValueColumn(col, category, idx)
    if (!best || s > best.score) best = { name: col, score: s }
  })
  return (best as { name: string; score: number } | null)?.name || numericCols[0]
}

function addSeries() {
  if (!Array.isArray(encoding.series)) encoding.series = []
  encoding.series.push({ name: `Series ${encoding.series.length + 1}`, value: '' })
}
function removeSeries(idx: number) {
  if (!Array.isArray(encoding.series)) return
  encoding.series.splice(idx, 1)
}
function addDimension() {
  dimensions.value = [...dimensions.value, '']
}
function removeDimension(idx: number) {
  const next = [...dimensions.value]
  next.splice(idx, 1)
  dimensions.value = next
}

function toViewPayload() {
  const t = local.type
  const caps = capsForType.value || {}
  
  // Base view with type
  const view: any = { type: t }
  
  // v2 Palette (for charts that support it)
  if (['bar_chart', 'line_chart', 'area_chart', 'pie_chart', 'scatter_plot', 'count', 'metric_card'].includes(t)) {
    view.palette = {
      theme: local.paletteTheme || 'default',
      scale: local.paletteScale || 'primary',
    }
  }
  
  // v2 Legend (nested object) - add for chart types that support legends
  if (['bar_chart', 'line_chart', 'area_chart', 'pie_chart', 'radar_chart'].includes(t)) {
    view.legend = {
      show: local.legendVisible ?? false,
      position: local.legendPosition || 'bottom',
    }
  }
  
  // v2 Axis options (nested objects) - for charts with axes
  if (['bar_chart', 'line_chart', 'area_chart', 'scatter_plot', 'heatmap'].includes(t)) {
    view.axisX = {
      show: local.xAxisVisible ?? true,
      rotate: local.xAxisRotate ?? 45,
      interval: local.xAxisInterval ?? 0,
    }
    view.axisY = {
      show: local.yAxisVisible ?? true,
      rotate: local.yAxisRotate ?? 0,
      interval: local.yAxisInterval ?? 0,
    }
  }
  
  // v2 Grid - for cartesian charts
  if (['bar_chart', 'line_chart', 'area_chart'].includes(t)) {
    view.showGrid = local.showGrid ?? true
  }
  
  // Chart-specific v2 fields
  if (['bar_chart', 'line_chart', 'area_chart'].includes(t)) {
    // Transform legacy encoding to v2 format
    const enc = deepClone(encoding)
    // x = category column
    view.x = enc.category || ''
    // y = array of value columns from series
    if (Array.isArray(enc.series) && enc.series.length) {
      const yValues = enc.series.map((s: any) => s.value).filter(Boolean)
      view.y = yValues.length === 1 ? yValues[0] : yValues
    }
    view.stacked = local.stacked ?? false
    view.smooth = local.smooth ?? false
    view.showDataZoom = local.showDataZoom ?? false
    
    // Bar chart specific
    if (t === 'bar_chart') {
      view.horizontal = local.horizontal ?? false
    }
    
    // Keep legacy encoding for backward compatibility
    if (enc.category && Array.isArray(enc.series) && enc.series.length) {
      enc.series = enc.series.map((s: any) => ({ ...s, key: enc.category }))
    }
    view.encoding = enc
  } else if (t === 'pie_chart') {
    const enc = deepClone(encoding)
    view.category = enc.category || ''
    view.value = enc.value || ''
    view.donut = local.donut ?? false
    if (local.donut) {
      view.innerRadius = 0.6
    }
    view.showLabels = true
    // Keep legacy encoding
    view.encoding = enc
  } else if (t === 'scatter_plot') {
    const enc = deepClone(encoding)
    view.x = enc.x || ''
    view.y = enc.y || ''
    view.size = enc.size || undefined
    view.colorBy = enc.color || undefined
    view.encoding = enc
  } else if (t === 'heatmap') {
    const enc = deepClone(encoding)
    view.x = enc.x || ''
    view.y = enc.y || ''
    view.value = enc.value || ''
    view.colorScheme = local.colorScheme || 'blue'
    view.showValues = local.showValues ?? true
    view.encoding = enc
  } else if (t === 'count' || t === 'metric_card') {
    const enc = deepClone(encoding)
    view.value = enc.value || ''
    view.format = 'number'
    view.encoding = enc
  } else if (t === 'table') {
    // Table doesn't need much
  } else {
    // Fallback for other types (candlestick, treemap, radar, etc.)
    if (showEncoding.value) {
      view.encoding = deepClone(encoding)
    }
  }
  
  // Legacy fields for backward compatibility
  if (local.style && Object.keys(local.style).length) {
    view.style = local.style
  }
  if (local.options && Object.keys(local.options).length) {
    view.options = local.options
  }
  if (local.variant) {
    view.variant = local.variant
  }
  
  return view
}

function validate(): string | null {
  const t = local.type
  const e = encoding
  // Minimal validation per type
  if (['bar_chart','line_chart','area_chart'].includes(t)) {
    if (!e.category) return 'Category is required'
    if (!Array.isArray(e.series) || !e.series.length) return 'At least one series is required'
    if (e.series.some((s: any) => !s.value)) return 'Each series must have a value column'
  } else if (t === 'pie_chart') {
    if (!e.category || !e.value) return 'Category and value are required'
  } else if (t === 'scatter_plot') {
    if (!e.x || !e.y) return 'X and Y are required'
  } else if (t === 'heatmap') {
    if (!e.x || !e.y || !e.value) return 'X, Y and value are required'
  } else if (t === 'candlestick') {
    if (!e.key || !e.open || !e.close || !e.low || !e.high) return 'Key, open, close, low, high are required'
  } else if (t === 'treemap') {
    if (!e.name || !e.value) return 'Name and value are required'
  } else if (t === 'radar_chart') {
    if (!Array.isArray(e.dimensions) || !e.dimensions.length) return 'At least one dimension is required'
  }
  return null
}

function apply() {
  error.value = ''
  const err = validate()
  if (err) { error.value = err; return }
  emit('apply', toViewPayload())
}

async function save() {
  error.value = ''
  const err = validate()
  if (err) { error.value = err; return }
  try {
    saving.value = true
    const { data, error: fe } = await useMyFetch(`/api/visualizations/${props.viz.id}`, {
      method: 'PATCH',
      body: { view: toViewPayload() }
    })
    if (fe.value) throw fe.value
    const updated = data.value
    emit('saved', updated)
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to save visualization'
  } finally {
    saving.value = false
  }
}

function reset() {
  const fresh = initFromView(props.viz?.view, props.step)
  const { encoding: freshEnc, ...rest } = fresh
  // Update local (except encoding placeholder)
  Object.assign(local, rest)
  // Sync encoding reactive in-place
  Object.keys(encoding).forEach(k => delete encoding[k])
  Object.assign(encoding, freshEnc || {})
}

watch(() => props.viz?.view, (v) => {
  if (!v) return
  // When parent replaces viz.view (e.g., after save), sync local state using v2-aware init
  const fresh = initFromView(v, props.step)
  const { encoding: freshEnc, ...rest } = fresh
  // Update local (except encoding placeholder)
  Object.assign(local, rest)
  // Sync encoding reactive in-place
  Object.keys(encoding).forEach(k => delete encoding[k])
  Object.assign(encoding, freshEnc || {})
  // Auto-detect missing encoding pieces after sync
  try {
    const t = local.type
    const need = (['bar_chart','line_chart','area_chart'].includes(t) && (!Array.isArray(encoding.series) || !encoding.series.length))
      || (t === 'pie_chart' && (!encoding.category || !encoding.value))
      || (t === 'scatter_plot' && (!encoding.x || !encoding.y))
      || (t === 'heatmap' && (!encoding.x || !encoding.y || !encoding.value))
      || (t === 'candlestick' && (!encoding.key || !encoding.open || !encoding.close || !encoding.low || !encoding.high))
      || (t === 'treemap' && (!encoding.name || !encoding.value))
      || (t === 'radar_chart' && (!Array.isArray(encoding.dimensions) || !encoding.dimensions.length))
    if (need) detectEncoding()
  } catch {}
})

// Initial auto-detect on mount if encoding incomplete
onMounted(() => {
  // Fetch backend visualization meta to drive capabilities and type list
  useMyFetch('/api/visualizations/meta').then((resp: any) => {
    try {
      const m = resp?.data?.value
      if (m && typeof m === 'object') meta.value = m
    } catch {}
  })
  try {
    const t = local.type
    const e: any = encoding
    const need = (['bar_chart','line_chart','area_chart'].includes(t) && (!Array.isArray(e.series) || !e.series.length))
      || (t === 'pie_chart' && (!e.category || !e.value))
      || (t === 'scatter_plot' && (!e.x || !e.y))
      || (t === 'heatmap' && (!e.x || !e.y || !e.value))
      || (t === 'candlestick' && (!e.key || !e.open || !e.close || !e.low || !e.high))
      || (t === 'treemap' && (!e.name || !e.value))
      || (t === 'radar_chart' && (!Array.isArray(e.dimensions) || !e.dimensions.length))
    if (need) detectEncoding()
  } catch {}
})

function detectEncoding() {
  const t = local.type
  const cols = allColumns.value
  const str = stringColumns.value
  const num = numericColumns.value
  if (!cols.length) return
  if (['bar_chart','line_chart','area_chart'].includes(t)) {
    encoding.category = encoding.category || str[0] || cols[0]
    const best = pickBestNumericValue(encoding.category, num, cols) || num[0] || cols[1]
    if (!Array.isArray(encoding.series) || !encoding.series.length) {
      encoding.series = [{ name: 'Series 1', value: best }]
    } else {
      // If the existing value is clearly wrong (equals category or looks like an ID), replace with best
      const v0 = String(encoding.series[0]?.value || '')
      if (!v0 || v0.toLowerCase() === String(encoding.category || '').toLowerCase() || /(^id$|_id$|id$)/i.test(v0)) {
        encoding.series[0] = { ...(encoding.series[0] || {}), value: best }
      }
    }
  } else if (t === 'pie_chart') {
    encoding.category = encoding.category || str[0] || cols[0]
    encoding.value = encoding.value || num[0] || cols[1]
  } else if (t === 'scatter_plot') {
    encoding.x = encoding.x || num[0] || cols[0]
    encoding.y = encoding.y || num[1] || cols[1]
  } else if (t === 'heatmap') {
    encoding.x = encoding.x || str[0] || cols[0]
    encoding.y = encoding.y || str[1] || cols[1]
    encoding.value = encoding.value || num[0] || cols[2]
  } else if (t === 'candlestick') {
    encoding.key = encoding.key || str[0] || cols[0]
    encoding.open = encoding.open || num[0] || cols[1]
    encoding.close = encoding.close || num[1] || cols[2]
    encoding.low = encoding.low || num[2] || cols[3]
    encoding.high = encoding.high || num[3] || cols[4]
  } else if (t === 'treemap') {
    encoding.name = encoding.name || str[0] || cols[0]
    encoding.value = encoding.value || num[0] || cols[1]
  } else if (t === 'radar_chart') {
    encoding.key = encoding.key || str[0] || cols[0]
    if (!Array.isArray(encoding.dimensions) || !encoding.dimensions.length) encoding.dimensions = num.slice(0, 3)
  }
}

// When switching type between bar/line/area, ensure view.type updates and variant resets appropriately
watch(() => local.type, (next, prev) => {
  // When switching to table-like, clear encoding and variants
  if (next === 'table' || next === 'count') {
    Object.keys(encoding).forEach(k => delete (encoding as any)[k])
    local.variant = null
    return
  }
  // For visual types, auto-detect minimal encoding if missing
  if (['bar_chart','line_chart','area_chart','pie_chart','scatter_plot','heatmap','candlestick','treemap','radar_chart'].includes(next)) {
    detectEncoding()
    if (next === 'area_chart') {
      local.variant = 'area'
    } else if (next === 'line_chart' && local.variant === 'area') {
      local.variant = null
    } else if (next === 'bar_chart') {
      local.variant = null
    }
  }
})

// Auto-expand axis sections when toggles are enabled
watch(() => local.xAxisVisible, (visible) => {
  if (visible && isType(['bar_chart','line_chart','area_chart','scatter_plot','heatmap'])) {
    expanded.xAxisLabels = true
  }
})

watch(() => local.yAxisVisible, (visible) => {
  if (visible && isType(['bar_chart','line_chart','area_chart','scatter_plot','heatmap'])) {
    expanded.yAxisLabels = true
  }
})
</script>

<style scoped>
</style>


