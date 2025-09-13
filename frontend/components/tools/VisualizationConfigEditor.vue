<template>
  <div class="space-y-3 text-xs text-gray-700">
    <!-- Type selector -->
    <div>
      <div class="font-medium text-gray-800 mb-1">Type</div>
      <select v-model="local.type" class="w-full border rounded px-2 py-1.5 bg-white">
        <option v-for="opt in typeOptions" :key="opt" :value="opt">{{ opt }}</option>
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
        <div class="flex items-center space-x-3 flex-wrap">
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.titleVisible" />
            <span>Title</span>
          </label>
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.legendVisible" />
            <span>Legend</span>
          </label>
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
        <div class="flex items-center space-x-3">
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.titleVisible" />
            <span>Title</span>
          </label>
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
        <div class="flex items-center space-x-3">
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.titleVisible" />
            <span>Title</span>
          </label>
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
        <div class="flex items-center space-x-3">
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.titleVisible" />
            <span>Title</span>
          </label>
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
        <div class="flex items-center space-x-3">
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.titleVisible" />
            <span>Title</span>
          </label>
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
        <div class="flex items-center space-x-3">
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.titleVisible" />
            <span>Title</span>
          </label>
          <label class="flex items-center space-x-1">
            <input type="checkbox" v-model="local.legendVisible" />
            <span>Legend</span>
          </label>
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
          <div class="grid grid-cols-2 gap-2">
            <div>
              <div class="text-gray-600 mb-1">Background</div>
              <input v-model="local.style.backgroundColor" placeholder="#ffffff" class="w-full border rounded px-2 py-1" />
            </div>
            <div>
              <div class="text-gray-600 mb-1">Title color</div>
              <input v-model="local.style.titleColor" placeholder="#111827" class="w-full border rounded px-2 py-1" />
            </div>
            <div>
              <div class="text-gray-600 mb-1">Title size</div>
              <input v-model.number="local.style.titleSize" type="number" min="10" max="36" class="w-full border rounded px-2 py-1" />
            </div>
            <div>
              <div class="text-gray-600 mb-1">Card background</div>
              <input v-model="local.style.cardBackground" placeholder="#ffffff or transparent" class="w-full border rounded px-2 py-1" />
            </div>
            <div>
              <div class="text-gray-600 mb-1">Card border (color or 'none')</div>
              <input v-model="local.style.cardBorder" placeholder="#e5e7eb or none" class="w-full border rounded px-2 py-1" />
            </div>
          </div>

          <!-- Axis label controls -->
          <div class="grid grid-cols-2 gap-2" v-if="isType(['bar_chart','line_chart','area_chart','scatter_plot','heatmap'])">
            <div>
              <div class="text-gray-600 mb-1">Label rotation</div>
              <select v-model.number="local.xAxisLabelRotate" class="w-full border rounded px-2 py-1 bg-white">
                <option :value="null">Auto</option>
                <option :value="0">0° (horizontal)</option>
                <option :value="45">45° (diagonal)</option>
                <option :value="90">90° (vertical)</option>
                <option :value="-45">-45° (diagonal)</option>
              </select>
            </div>
            <div>
              <div class="text-gray-600 mb-1">Label interval</div>
              <select v-model.number="local.xAxisLabelInterval" class="w-full border rounded px-2 py-1 bg-white">
                <option :value="null">Auto</option>
                <option :value="0">Show all (0)</option>
                <option :value="1">Every 2nd (1)</option>
                <option :value="2">Every 3rd (2)</option>
                <option :value="3">Every 4th (3)</option>
              </select>
            </div>
          </div>

          <!-- Visibility toggles -->
          <div class="flex items-center space-x-3 flex-wrap">
            <label class="flex items-center space-x-1">
              <input type="checkbox" v-model="local.titleVisible" />
              <span>Title</span>
            </label>
            <label class="flex items-center space-x-1">
              <input type="checkbox" v-model="local.legendVisible" />
              <span>Legend</span>
            </label>
            <label class="flex items-center space-x-1">
              <input type="checkbox" v-model="local.xAxisVisible" />
              <span>X Axis</span>
            </label>
            <label class="flex items-center space-x-1">
              <input type="checkbox" v-model="local.yAxisVisible" />
              <span>Y Axis</span>
            </label>
            <label class="flex items-center space-x-1">
              <input type="checkbox" v-model="local.showGridLines" />
              <span>Grid lines</span>
            </label>
          </div>
        </div>
      </Transition>
    </div>

    <!-- Actions -->
    <div class="pt-2 border-t flex items-center justify-end space-x-2">
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

const saving = ref(false)
const error = ref('')

// UI section expand/collapse state
const expanded = reactive<{ typeData: boolean; style: boolean }>({ typeData: true, style: true })

function deepClone<T>(v: T): T { return JSON.parse(JSON.stringify(v || {})) }

const local = reactive<any>({
  type: props.viz?.view?.type || props.step?.data_model?.type || 'table',
  encoding: deepClone(props.viz?.view?.encoding || {}),
  titleVisible: props.viz?.view?.titleVisible ?? true,
  legendVisible: props.viz?.view?.legendVisible ?? false,
  xAxisVisible: props.viz?.view?.xAxisVisible ?? true,
  yAxisVisible: props.viz?.view?.yAxisVisible ?? true,
  variant: props.viz?.view?.variant || null,
  style: deepClone(props.viz?.view?.style || {}),
  options: deepClone(props.viz?.view?.options || {}),
  // X-axis label controls
  xAxisLabelRotate: props.viz?.view?.xAxisLabelRotate ?? null,
  xAxisLabelInterval: props.viz?.view?.xAxisLabelInterval ?? null,
  showGridLines: props.viz?.view?.showGridLines ?? null,
})

// Convenience accessors bound to encoding structure
const encoding = reactive<any>(local.encoding)
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
  const view: any = {
    type: local.type,
    titleVisible: local.titleVisible,
    legendVisible: local.legendVisible,
    xAxisVisible: local.xAxisVisible,
    yAxisVisible: local.yAxisVisible,
    variant: local.variant || undefined,
    style: local.style || {},
    options: local.options || {},
    // X-axis label controls
    xAxisLabelRotate: local.xAxisLabelRotate,
    xAxisLabelInterval: local.xAxisLabelInterval,
    showGridLines: local.showGridLines,
  }
  if (showEncoding.value) {
    const enc = deepClone(encoding)
    // Normalize bar/line/area to ensure series[].key strictly matches category
    if (["bar_chart","line_chart","area_chart"].includes(local.type)) {
      if (enc.category && Array.isArray(enc.series) && enc.series.length) {
        enc.series = enc.series.map((s: any) => ({ ...s, key: enc.category }))
      }
    }
    // Normalize pie to either (category,value) or explicit series with key
    if (local.type === 'pie_chart') {
      const cat = enc.category
      const val = enc.value || (Array.isArray(enc.series) && enc.series[0]?.value)
      const name = enc.name || (Array.isArray(enc.series) && enc.series[0]?.name) || 'Series 1'
      if (cat && val) {
        enc.series = [{ name, key: cat, value: val }]
      } else if (Array.isArray(enc.series) && enc.series.length) {
        enc.series = enc.series.map((s: any) => ({ ...s, key: s.key || cat }))
      }
    }
    view.encoding = enc
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
  const base = props.viz?.view || {}
  local.type = base.type || props.step?.data_model?.type || 'table'
  local.titleVisible = base.titleVisible ?? true
  local.legendVisible = base.legendVisible ?? false
  local.xAxisVisible = base.xAxisVisible ?? true
  local.yAxisVisible = base.yAxisVisible ?? true
  local.variant = base.variant || null
  local.style = deepClone(base.style || {})
  local.options = deepClone(base.options || {})
  local.xAxisLabelRotate = base.xAxisLabelRotate ?? null
  local.xAxisLabelInterval = base.xAxisLabelInterval ?? null
  const e = deepClone(base.encoding || {})
  Object.keys(encoding).forEach(k => delete (encoding as any)[k])
  Object.assign(encoding, e)
}

watch(() => props.viz?.view, (v) => {
  if (!v) return
  // When parent replaces viz.view (e.g., after save), sync local state
  local.type = v.type || local.type
  local.titleVisible = v.titleVisible ?? local.titleVisible
  local.legendVisible = v.legendVisible ?? local.legendVisible
  local.xAxisVisible = v.xAxisVisible ?? local.xAxisVisible
  local.yAxisVisible = v.yAxisVisible ?? local.yAxisVisible
  local.variant = v.variant || local.variant
  local.style = deepClone(v.style || local.style)
  local.options = deepClone(v.options || local.options)
  local.xAxisLabelRotate = v.xAxisLabelRotate ?? local.xAxisLabelRotate
  local.xAxisLabelInterval = v.xAxisLabelInterval ?? local.xAxisLabelInterval
  const e = deepClone(v.encoding || {})
  Object.keys(encoding).forEach(k => delete (encoding as any)[k])
  Object.assign(encoding, e)
  // Auto-detect missing encoding pieces after sync
  try {
    const t = local.type
    const e2: any = encoding
    const need = (['bar_chart','line_chart','area_chart'].includes(t) && (!Array.isArray(e2.series) || !e2.series.length))
      || (t === 'pie_chart' && (!e2.category || !e2.value))
      || (t === 'scatter_plot' && (!e2.x || !e2.y))
      || (t === 'heatmap' && (!e2.x || !e2.y || !e2.value))
      || (t === 'candlestick' && (!e2.key || !e2.open || !e2.close || !e2.low || !e2.high))
      || (t === 'treemap' && (!e2.name || !e2.value))
      || (t === 'radar_chart' && (!Array.isArray(e2.dimensions) || !e2.dimensions.length))
    if (need) detectEncoding()
  } catch {}
})

// Initial auto-detect on mount if encoding incomplete
onMounted(() => {
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
    if (!Array.isArray(encoding.series) || !encoding.series.length) encoding.series = [{ name: 'Series 1', value: num[0] || cols[1] }]
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
</script>

<style scoped>
</style>


