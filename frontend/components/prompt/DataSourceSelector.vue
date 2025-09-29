<template>
    <div class="inline-block relative" ref="containerRef">
        <UPopover :popper="{ strategy: 'absolute', placement: 'bottom-start', offset: [0,8] }">
            <UTooltip :text="isCompactFinal ? dataTooltip : ''" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
                <button class="inline-flex items-center text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md p-2 text-xs">
                    <span v-if="internalSelectedDataSources.length > 0" class="flex items-center">
                        <template v-if="isCompactFinal">
                            <!-- Compact: show only first icon -->
                            <DataSourceIcon :type="internalSelectedDataSources[0].type" class="h-4 w-4" />
                        </template>
                        <template v-else>
                            <!-- Non-compact: show all icons with spacing -->
                            <DataSourceIcon 
                                v-for="(ds, index) in internalSelectedDataSources" 
                                :key="ds.id" 
                                :type="ds.type" 
                                :class="`w-4 ${index > 0 ? 'ml-0.5' : ''}`"
                            />
                        </template>
                    </span>
                    <span v-else class="flex items-center">
                        <Icon name="heroicons-circle-stack" class="h-4 w-4" />
                    </span>
                </button>
            </UTooltip>
            <template #panel>
                <div class="p-2 text-xs max-h-64 overflow-y-auto w-[260px] rounded-xl">
                    <div v-for="ds in dataSources" :key="ds.id" class="px-2 py-1.5 rounded hover:bg-gray-50 cursor-pointer flex items-center justify-between" @click="() => { toggleDataSource(ds); }">
                        <div class="flex items-center">
                            <DataSourceIcon :type="ds.type" class="w-4" />
                            <span class="ml-2 text-[13px]">{{ ds.name }}</span>
                        </div>
                        <Icon v-if="isSelected(ds)" name="heroicons-check" class="w-4 h-4 text-blue-500" />
                    </div>
                </div>
            </template>
        </UPopover>
    </div>
    
</template>

<script lang="ts" setup>

type DataSource = { id: string; name: string; type?: string }
const internalSelectedDataSources = ref<DataSource[]>([])
const dataSources = ref<DataSource[]>([])
const isOpen = ref(false)
const containerRef = ref<HTMLElement | null>(null)
const isCompact = ref(false)
const isCompactFinal = computed(() => isCompact.value)

const props = defineProps({
    selectedDataSources: {
        type: Array,
        default: () => [],
    },
    reportId: {
        type: String,
        default: () => '',
    }
});

const emit = defineEmits(['update:selectedDataSources']);

async function getDataSources() {
    const response = await useMyFetch('/data_sources/active', {
        method: 'GET',
    }).then((response) => {
        dataSources.value = (response.data.value as any[]) || []
    })
    // Initialize selection from prop if provided, otherwise leave empty for parent to decide
    if ((props.selectedDataSources as any[])?.length) {
        // Align to the objects from the current dataSources list by id
        const ids = new Set((props.selectedDataSources as any[]).map((x: any) => x.id))
        internalSelectedDataSources.value = dataSources.value.filter((ds: any) => ids.has(ds.id))
        handleSelectionChange()
    } else if (!props.reportId) {
        // Landing page (no report): default to ALL active data sources
        internalSelectedDataSources.value = dataSources.value
        handleSelectionChange()
    }

}

function handleSelectionChange() {
    emit('update:selectedDataSources', internalSelectedDataSources.value);
}

function isSelected(option: any) {
    return internalSelectedDataSources.value.some((ds: any) => ds.id === option.id)
}

function toggleDataSource(ds: DataSource) {
    const exists = internalSelectedDataSources.value.find((x) => x.id === ds.id)
    if (exists) {
        internalSelectedDataSources.value = internalSelectedDataSources.value.filter((x) => x.id !== ds.id)
    } else {
        internalSelectedDataSources.value = [...internalSelectedDataSources.value, ds]
    }
    handleSelectionChange()
    // If we are in a report context, persist selection at report level immediately
    persistSelectionIfReport()
}

onMounted(() => {
    nextTick(async () => {
        const { organization, ensureOrganization } = useOrganization()
        
        try {
            // Wait for organization to be available before making API calls
            await ensureOrganization()
            
            if (organization.value?.id) {
                getDataSources()
            } else {
                console.warn('DataSourceSelectorComponentExcel: Organization not available, skipping API calls')
            }
        } catch (error) {
            console.error('DataSourceSelectorComponentExcel: Error during initialization:', error)
        }
        // Setup resize observer for compact mode
        // Look for the nearest parent container that's likely the prompt box
        const findPromptContainer = () => {
            let parent = containerRef.value?.parentElement
            while (parent && parent.clientWidth < 300) {
                parent = parent.parentElement
            }
            return parent || containerRef.value
        }
        
        const ro = new ResizeObserver(() => {
            const targetEl = findPromptContainer()
            const w = targetEl?.clientWidth || 0
            // Use a more reasonable threshold - compact if container is less than 420px
            isCompact.value = w > 0 && w < 420
        })
        
        // Observe the container initially, then try to find a better parent
        if (containerRef.value) {
            ro.observe(containerRef.value)
            // Also try to observe a parent container after a short delay
            setTimeout(() => {
                const betterTarget = findPromptContainer()
                if (betterTarget && betterTarget !== containerRef.value) {
                    ro.unobserve(containerRef.value!)
                    ro.observe(betterTarget)
                }
            }, 100)
        }
    })
})
// Keep internal selection in sync with parent-provided selectedDataSources
watch(() => props.selectedDataSources, (newVal: any[]) => {
    if (!Array.isArray(newVal)) return
    const ids = new Set(newVal.map((x: any) => x.id))
    // Map to known dataSources, or fall back to the raw objects if not present yet
    const mapped = dataSources.value.length
        ? dataSources.value.filter((ds: any) => ids.has(ds.id))
        : newVal
    internalSelectedDataSources.value = mapped as any
}, { immediate: true, deep: true })

async function persistSelectionIfReport() {
    try {
        if (!props.reportId) return
        const ids = internalSelectedDataSources.value.map((x: any) => x.id)
        await useMyFetch(`/reports/${props.reportId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ data_sources: ids })
        })
    } catch (e) {
        console.error('Failed to update report data sources:', e)
    }
}
const dataTooltip = computed<string>(() => {
    if (internalSelectedDataSources.value.length <= 1) return ''
    const rest = internalSelectedDataSources.value.slice(1).map(s => s.name).join(', ')
    return rest
})

</script>