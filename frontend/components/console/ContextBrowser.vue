<template>
  <div class="space-y-3">
    <!-- Schemas Section (object-first; fallback to legacy XML) -->
    <div>
      <div 
        class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2"
        @click="toggleSection('schemas')"
      >
        <Icon :name="expandedSections.has('schemas') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Schemas
      </div>
      <Transition name="fade">
        <div v-if="expandedSections.has('schemas')" class="ml-2 space-y-2">
          <!-- Object-based data sources and tables -->
          <div v-if="dataSources.length" class="space-y-2">
            <div v-for="ds in dataSources" :key="ds.info.id" class="border rounded-md">
              <div class="flex items-center justify-between px-3 py-2 bg-gray-50 cursor-pointer" @click="toggleSection('ds:'+ds.info.id)">
                <div class="text-xs font-medium text-gray-900">
                  {{ ds.info.name }} <span class="text-gray-500">({{ ds.info.type }})</span>
                </div>
                <Icon :name="expandedSections.has('ds:'+ds.info.id) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-500" />
              </div>
              <Transition name="fade">
                <div v-if="expandedSections.has('ds:'+ds.info.id)" class="px-3 py-2 space-y-1">
                  <div v-if="ds.info.context" class="text-[11px] text-gray-600">{{ ds.info.context }}</div>
                  <div class="text-[11px] uppercase tracking-wide text-gray-500 mt-2">Schemas</div>
                  <div class="divide-y">
                    <div v-for="tbl in (ds.tables || [])" :key="ds.info.id + ':' + tbl.name" class="py-1">
                      <div class="flex items-center cursor-pointer" @click="toggleSection('tbl:'+ds.info.id+':'+tbl.name)">
                        <div class="text-xs text-gray-900">{{ tbl.name }}</div>
                        <div class="ml-auto flex items-center gap-3">
                          <!-- Score -->
                          <div v-if="tbl.score !== undefined" class="text-[10px] text-gray-500">score: {{ formatScore(tbl.score) }}</div>
                          <!-- Usage total -->
                          <div v-if="tbl.usage_count !== undefined" class="text-[10px] text-gray-500">usage: {{ tbl.usage_count }}</div>
                          <!-- Success / Failure with icons -->
                          <div v-if="tbl.success_count !== undefined || tbl.failure_count !== undefined" class="flex items-center gap-2">
                            <span class="inline-flex items-center text-[10px] text-gray-700">
                              <Icon name="heroicons-check-circle" class="w-3 h-3 text-green-600 mr-1" />
                              {{ tbl.success_count ?? 0 }}
                            </span>
                            <span class="inline-flex items-center text-[10px] text-gray-700">
                              <Icon name="heroicons-x-circle" class="w-3 h-3 text-red-600 mr-1" />
                              {{ tbl.failure_count ?? 0 }}
                            </span>
                          </div>
                          <!-- Feedback thumbs up/down -->
                          <div v-if="tbl.pos_feedback_count !== undefined || tbl.neg_feedback_count !== undefined" class="flex items-center gap-2">
                            <span class="inline-flex items-center text-[10px] text-gray-700">
                              <Icon name="heroicons-hand-thumb-up" class="w-3 h-3 text-green-600 mr-1" />
                              {{ tbl.pos_feedback_count ?? 0 }}
                            </span>
                            <span class="inline-flex items-center text-[10px] text-gray-700">
                              <Icon name="heroicons-hand-thumb-down" class="w-3 h-3 text-red-600 mr-1" />
                              {{ tbl.neg_feedback_count ?? 0 }}
                            </span>
                          </div>
                          <Icon :name="expandedSections.has('tbl:'+ds.info.id+':'+tbl.name) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-500" />
                        </div>
                      </div>
                      <Transition name="fade">
                        <div v-if="expandedSections.has('tbl:'+ds.info.id+':'+tbl.name)" class="mt-1 pl-2 space-y-2">
                          <div>
                            <div class="text-[11px] text-gray-700">Columns:</div>
                            <ul class="ml-3 list-disc">
                              <li v-for="col in (tbl.columns || [])" :key="tbl.name + ':' + col.name" class="text-[11px] text-gray-800">
                                {{ col.name }}<span v-if="col.dtype" class="text-gray-500"> ({{ col.dtype }})</span>
                              </li>
                            </ul>
                          </div>
                          <div v-if="tableMetrics(tbl).length">
                            <div class="text-[11px] text-gray-700">Metrics:</div>
                            <ul class="ml-3 list-disc">
                              <li v-for="m in tableMetrics(tbl)" :key="m.key" class="text-[11px] text-gray-800">
                                {{ m.label }}: <span class="text-gray-600">{{ m.value }}</span>
                              </li>
                            </ul>
                          </div>
                        </div>
                      </Transition>
                    </div>
                  </div>
                </div>
              </Transition>
            </div>
          </div>

          <!-- Fallback: legacy XML-like string rendering -->
          <div v-else-if="schemasText">
            <div v-for="section in xmlSections" :key="section.tag" class="text-xs">
              <div class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-1" @click="toggleSection('tag:'+section.tag)">
                <Icon :name="expandedSections.has('tag:'+section.tag) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
                {{ section.tag }}
                <span v-if="section.tag === 'schema' && tables.length" class="ml-2 text-[11px] text-gray-400">({{ tables.length }} tables)</span>
              </div>
              <Transition name="fade">
                <div v-if="expandedSections.has('tag:'+section.tag) && section.tag !== 'schema'" class="ml-3">
                  <pre class="text-xs text-gray-700 whitespace-pre-wrap">{{ section.content.trim() }}</pre>
                </div>
              </Transition>
              <Transition name="fade">
                <div v-if="expandedSections.has('tag:'+section.tag) && section.tag === 'schema'" class="ml-2 space-y-2">
                  <div v-for="t in tables" :key="t.name">
                    <div class="flex items-center flex-wrap gap-x-2 cursor-pointer text-[11px] uppercase tracking-wide text-gray-500" @click="toggleSection('table:'+t.name)">
                      <Icon :name="expandedSections.has('table:'+t.name) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
                      <span class="inline-flex items-center">
                        <span :class="['inline-block w-2.5 h-2.5 rounded-full mr-1', scoreDotClass(t.metrics?.score)]"></span>
                        <span class="font-medium">{{ t.name }}</span>
                        <span class="ml-2 text-gray-400 normal-case">(
                          {{ t.columns?.length || 0 }} columns
                          <template v-if="t.metrics && t.metrics.score !== undefined">, score: {{ formatScore(t.metrics.score) }}</template>
                        )</span>
                      </span>
                    </div>
                    <Transition name="fade">
                      <div v-if="expandedSections.has('table:'+t.name)" class="ml-4 mt-1 space-y-1">
                        <div v-if="t.columns?.length">
                          <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Columns</div>
                          <div class="grid grid-cols-1 gap-1">
                            <div v-for="c in t.columns" :key="c.name + ':' + c.type">
                              <span class="font-mono text-gray-800">{{ c.name }}</span>
                              <span class="text-gray-500">: {{ c.type }}</span>
                            </div>
                          </div>
                        </div>
                        <div v-if="t.metrics && Object.keys(t.metrics).length" class="mt-2">
                          <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Metrics</div>
                          <div class="text-xs">
                            <div v-for="m in formatMetricsList(t.metrics)" :key="m.key" :class="m.class">
                              {{ m.label }}: <span class="text-gray-900">{{ m.value }}</span>
                            </div>
                          </div>
                        </div>
                      </div>
                    </Transition>
                  </div>
                </div>
              </Transition>
            </div>
          </div>
          <!-- No schemas -->
          <div v-else class="text-xs text-gray-500">No schemas available</div>
        </div>
      </Transition>
    </div>

    <!-- Instructions Section (object-first; fallback to legacy XML) -->
    <div v-if="instructionsItems.length || instructionsText">
      <div 
        class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2"
        @click="toggleSection('instructions')"
      >
        <Icon :name="expandedSections.has('instructions') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Instructions
      </div>
      <Transition name="fade">
        <div v-if="expandedSections.has('instructions')" class="ml-4 space-y-2">
          <div v-if="instructionsItems.length === 0 && !instructionsText" class="text-xs text-gray-500">No instructions</div>
          <!-- Object items -->
          <div v-for="ins in instructionsItems" :key="ins.id || ins.key">
            <div class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-1" @click="toggleSection('instruction:'+(ins.id || ins.key))">
              <Icon :name="expandedSections.has('instruction:'+(ins.id || ins.key)) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
              Instruction
              <span v-if="ins.category" class="ml-2 text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-600 normal-case">{{ ins.category }}</span>
              <span v-if="ins.id" class="ml-1 text-[10px] text-gray-400 normal-case">#{{ String(ins.id).slice(0,8) }}</span>
            </div>
            <Transition name="fade">
              <div v-if="expandedSections.has('instruction:'+(ins.id || ins.key))" class="ml-3">
                <pre class="text-xs text-gray-700 whitespace-pre-wrap">{{ ins.text || ins.content }}</pre>
              </div>
            </Transition>
          </div>
          <!-- Fallback legacy parsed list -->
          <div v-for="ins in instructionsList" :key="'legacy-'+ins.key">
            <div class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-1" @click="toggleSection('instruction:'+ins.key)">
              <Icon :name="expandedSections.has('instruction:'+ins.key) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
              Instruction
              <span v-if="ins.category" class="ml-2 text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-600 normal-case">{{ ins.category }}</span>
              <span v-if="ins.id" class="ml-1 text-[10px] text-gray-400 normal-case">#{{ ins.id.slice(0, 8) }}</span>
            </div>
            <Transition name="fade">
              <div v-if="expandedSections.has('instruction:'+ins.key)" class="ml-3">
                <pre class="text-xs text-gray-700 whitespace-pre-wrap">{{ ins.content }}</pre>
              </div>
            </Transition>
          </div>
        </div>
      </Transition>
    </div>

    <!-- Metadata Resources Section (object-first; fallback simple) -->
    <div v-if="resourcesContent || metadataResources.length > 0">
      <div 
        class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2"
        @click="toggleSection('metadata_resources')"
      >
        <Icon :name="expandedSections.has('metadata_resources') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Metadata Resources
      </div>
      <Transition name="fade">
        <div v-if="expandedSections.has('metadata_resources')" class="ml-4">
          <pre v-if="resourcesContent" class="text-xs text-gray-700 whitespace-pre-wrap">{{ resourcesContent }}</pre>
          <div v-else class="space-y-1">
            <div v-for="resource in metadataResources" :key="resource.name" class="text-xs">
              <div class="font-mono text-gray-800">{{ resource.name }}</div>
              <div class="text-gray-500 text-[11px] ml-2">{{ resource.type }}</div>
              <div v-if="resource.description" class="text-gray-500 text-[11px] ml-2">{{ resource.description }}</div>
            </div>
          </div>
        </div>
      </Transition>
    </div>

    <!-- Observations Section -->
    <div v-if="observations">
      <div 
        class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2"
        @click="toggleSection('observations')"
      >
        <Icon :name="expandedSections.has('observations') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Observations
        <span v-if="toolObservations.length" class="ml-2 text-[10px] text-gray-400">({{ toolObservations.length }} execution{{ toolObservations.length !== 1 ? 's' : '' }})</span>
      </div>
      <Transition name="fade">
        <div v-if="expandedSections.has('observations')" class="ml-2 space-y-2">
          <!-- No observations -->
          <div v-if="!toolObservations.length" class="text-xs text-gray-500">No tool executions recorded</div>
          
          <!-- Tool Observations List -->
          <div v-for="obs in toolObservations" :key="'obs-' + obs.execution_number" class="border rounded-md overflow-hidden">
            <!-- Observation Header -->
            <div 
              class="flex items-center justify-between px-3 py-2 cursor-pointer"
              :class="getObservationHeaderClass(obs)"
              @click="toggleSection('obs:' + obs.execution_number)"
            >
              <div class="flex items-center gap-2">
                <span class="inline-flex items-center justify-center w-5 h-5 rounded-full bg-gray-700 text-white text-[10px] font-medium">
                  {{ obs.execution_number }}
                </span>
                <span class="text-xs font-medium text-gray-900">{{ obs.tool_name }}</span>
                <Icon 
                  v-if="hasObservationError(obs)" 
                  name="heroicons-exclamation-triangle" 
                  class="w-3.5 h-3.5 text-red-500" 
                />
                <Icon 
                  v-else-if="hasObservationSuccess(obs)" 
                  name="heroicons-check-circle" 
                  class="w-3.5 h-3.5 text-green-500" 
                />
              </div>
              <div class="flex items-center gap-2">
                <span v-if="obs.timestamp" class="text-[10px] text-gray-400">{{ formatObservationTime(obs.timestamp) }}</span>
                <Icon :name="expandedSections.has('obs:' + obs.execution_number) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-500" />
              </div>
            </div>
            
            <!-- Observation Details (expanded) -->
            <Transition name="fade">
              <div v-if="expandedSections.has('obs:' + obs.execution_number)" class="px-3 py-2 bg-white border-t border-gray-100 space-y-3">
                <!-- Tool Input -->
                <div v-if="obs.tool_input && Object.keys(obs.tool_input).length">
                  <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1.5">Input</div>
                  
                  <!-- Title -->
                  <div v-if="obs.tool_input.title" class="mb-2">
                    <div class="text-xs font-medium text-gray-900">{{ obs.tool_input.title }}</div>
                  </div>
                  
                  <!-- User Prompt -->
                  <div v-if="obs.tool_input.user_prompt" class="mb-2">
                    <div class="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">User Prompt</div>
                    <div class="text-xs text-gray-700 bg-gray-50 rounded px-2 py-1.5 border-l-2 border-gray-300">
                      {{ truncateText(obs.tool_input.user_prompt, 200) }}
                    </div>
                  </div>
                  
                  <!-- Interpreted Prompt -->
                  <div v-if="obs.tool_input.interpreted_prompt" class="mb-2">
                    <div class="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">Interpreted</div>
                    <div class="text-xs text-gray-700 bg-blue-50 rounded px-2 py-1.5 border-l-2 border-blue-300">
                      {{ truncateText(obs.tool_input.interpreted_prompt, 200) }}
                    </div>
                  </div>
                  
                  <!-- Tables by Source -->
                  <div v-if="obs.tool_input.tables_by_source?.length" class="mb-2">
                    <div class="text-[10px] uppercase tracking-wide text-gray-400 mb-1">Tables</div>
                    <div class="flex flex-wrap gap-1">
                      <template v-for="source in obs.tool_input.tables_by_source" :key="source.data_source_id">
                        <span 
                          v-for="table in (source.tables || [])" 
                          :key="source.data_source_id + ':' + table"
                          class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] bg-indigo-50 text-indigo-700 border border-indigo-200"
                        >
                          <Icon name="heroicons-table-cells" class="w-3 h-3 mr-1 text-indigo-400" />
                          {{ table }}
                        </span>
                      </template>
                    </div>
                  </div>
                  
                  <!-- Other Input Fields (collapsible) -->
                  <div v-if="getOtherInputFields(obs.tool_input).length" class="mt-2">
                    <div 
                      class="flex items-center cursor-pointer text-[10px] uppercase tracking-wide text-gray-400 mb-1"
                      @click.stop="toggleSection('obs-input:' + obs.execution_number)"
                    >
                      <Icon :name="expandedSections.has('obs-input:' + obs.execution_number) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
                      Other Fields ({{ getOtherInputFields(obs.tool_input).length }})
                    </div>
                    <Transition name="fade">
                      <div v-if="expandedSections.has('obs-input:' + obs.execution_number)" class="ml-2 space-y-1">
                        <div v-for="[key, value] in getOtherInputFields(obs.tool_input)" :key="key" class="text-xs">
                          <span class="text-gray-500">{{ key }}:</span>
                          <span class="text-gray-800 ml-1">{{ formatInputValue(value) }}</span>
                        </div>
                      </div>
                    </Transition>
                  </div>
                </div>
                
                <!-- Observation Result -->
                <div v-if="obs.observation">
                  <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1.5">Result</div>
                  
                  <!-- Summary -->
                  <div v-if="obs.observation.summary" class="mb-2">
                    <div 
                      class="text-xs rounded px-2 py-1.5 border-l-2"
                      :class="hasObservationError(obs) ? 'bg-red-50 border-red-300 text-red-800' : 'bg-green-50 border-green-300 text-green-800'"
                    >
                      {{ obs.observation.summary }}
                    </div>
                  </div>
                  
                  <!-- Error Details -->
                  <div v-if="obs.observation.error" class="mb-2">
                    <div class="text-[10px] uppercase tracking-wide text-red-400 mb-0.5">Error</div>
                    <div class="text-xs text-red-700 bg-red-50 rounded px-2 py-1.5 border border-red-200">
                      <template v-if="typeof obs.observation.error === 'string'">
                        {{ obs.observation.error }}
                      </template>
                      <template v-else-if="obs.observation.error.message">
                        {{ obs.observation.error.message }}
                        <div v-if="obs.observation.error.field_errors?.length" class="mt-1 text-[10px]">
                          Fields: {{ obs.observation.error.field_errors.join(', ') }}
                        </div>
                        <div v-if="obs.observation.error.suggestion" class="mt-1 text-[10px] text-red-600">
                          Suggestion: {{ obs.observation.error.suggestion }}
                        </div>
                      </template>
                      <template v-else>
                        {{ formatJson(obs.observation.error) }}
                      </template>
                    </div>
                  </div>
                  
                  <!-- Other Observation Fields -->
                  <div v-if="getOtherObservationFields(obs.observation).length">
                    <div 
                      class="flex items-center cursor-pointer text-[10px] uppercase tracking-wide text-gray-400 mb-1"
                      @click.stop="toggleSection('obs-result:' + obs.execution_number)"
                    >
                      <Icon :name="expandedSections.has('obs-result:' + obs.execution_number) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
                      Details
                    </div>
                    <Transition name="fade">
                      <div v-if="expandedSections.has('obs-result:' + obs.execution_number)" class="ml-2">
                        <pre class="text-[11px] text-gray-700 whitespace-pre-wrap bg-gray-50 rounded p-2 overflow-x-auto max-h-40">{{ formatJson(Object.fromEntries(getOtherObservationFields(obs.observation))) }}</pre>
                      </div>
                    </Transition>
                  </div>
                </div>
              </div>
            </Transition>
          </div>
          
          <!-- Widget Updates Summary -->
          <div v-if="widgetUpdates.length" class="border rounded-md px-3 py-2 bg-purple-50 border-purple-200">
            <div class="flex items-center gap-2">
              <Icon name="heroicons-squares-2x2" class="w-4 h-4 text-purple-500" />
              <span class="text-xs font-medium text-purple-900">{{ widgetUpdates.length }} widget{{ widgetUpdates.length !== 1 ? 's' : '' }} created/updated</span>
            </div>
          </div>
          
          <!-- Visualization Updates Summary -->
          <div v-if="visualizationUpdates.length" class="border rounded-md px-3 py-2 bg-teal-50 border-teal-200">
            <div class="flex items-center gap-2">
              <Icon name="heroicons-chart-bar" class="w-4 h-4 text-teal-500" />
              <span class="text-xs font-medium text-teal-900">{{ visualizationUpdates.length }} visualization{{ visualizationUpdates.length !== 1 ? 's' : '' }} created/updated</span>
            </div>
          </div>
        </div>
      </Transition>
    </div>

    <!-- Mentions Section (object-based from warm context) -->
    <div v-if="mentions && (mentions.files?.length || mentions.data_sources?.length || mentions.tables?.length || mentions.entities?.length)">
      <div 
        class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2"
        @click="toggleSection('mentions')"
      >
        <Icon :name="expandedSections.has('mentions') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Mentions
      </div>
      <Transition name="fade">
        <div v-if="expandedSections.has('mentions')" class="ml-4 space-y-3">
          <!-- Files -->
          <div v-if="mentions.files?.length">
            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Files</div>
            <div class="space-y-1">
              <div v-for="f in mentions.files" :key="f.id" class="text-xs text-gray-800">
                <span class="font-mono">{{ f.filename || f.id }}</span>
                <span v-if="f.content_type" class="text-gray-500"> ({{ f.content_type }})</span>
              </div>
            </div>
          </div>

          <!-- Data Sources -->
          <div v-if="mentions.data_sources?.length">
            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Data Sources</div>
            <div class="space-y-1">
              <div v-for="ds in mentions.data_sources" :key="ds.id" class="text-xs text-gray-800">
                <span class="font-mono">{{ ds.name || ds.id }}</span>
              </div>
            </div>
          </div>

          <!-- Tables -->
          <div v-if="mentions.tables?.length">
            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Tables</div>
            <div class="space-y-1">
              <div v-for="t in mentions.tables" :key="t.id" class="text-xs text-gray-800">
                <div>
                  <span class="font-mono">{{ (t.data_source_name ? (t.data_source_name + '.') : '') + (t.table_name || '') }}</span>
                </div>
                <div v-if="t.columns_preview?.length" class="text-[11px] text-gray-600 ml-2">
                  columns: {{ t.columns_preview.join(', ') }}
                </div>
              </div>
            </div>
          </div>

          <!-- Entities -->
          <div v-if="mentions.entities?.length">
            <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Entities</div>
            <div class="space-y-2">
              <div v-for="e in mentions.entities" :key="e.id" class="text-xs text-gray-800">
                <div class="flex items-center flex-wrap gap-2">
                  <span class="font-medium">{{ e.title || e.id }}</span>
                  <span v-if="e.entity_type" class="text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-600">{{ e.entity_type }}</span>
                  <span v-if="e.status" class="text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-600">{{ e.status }}</span>
                </div>
                <div v-if="e.description" class="text-[11px] text-gray-600 mt-0.5">{{ (e.description || '').slice(0, 200) }}<span v-if="(e.description || '').length > 200">…</span></div>
                <div v-if="e.columns?.length" class="text-[11px] text-gray-600 mt-0.5">columns: {{ e.columns.join(', ') }}</div>
                <div v-if="e.sample_rows?.length" class="text-[11px] text-gray-600 mt-0.5">
                  sample rows:
                  <div class="ml-2">
                    <div v-for="(row, idx) in e.sample_rows.slice(0, 2)" :key="idx" class="text-[11px] text-gray-700">
                      {{ Object.entries(row).slice(0,6).map(([k,v]) => `${k}=${String(v).length > 100 ? String(v).slice(0,100)+'…' : v}`).join(', ') }}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </Transition>
    </div>

    <!-- Entities Section (object-based from warm context) -->
    <div v-if="entities && (entities.items?.length)">
      <div 
        class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2"
        @click="toggleSection('entities')"
      >
        <Icon :name="expandedSections.has('entities') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Entities
      </div>
      <Transition name="fade">
        <div v-if="expandedSections.has('entities')" class="ml-4 space-y-2">
          <div v-for="e in (entities.items || [])" :key="e.id" class="text-xs text-gray-800 border rounded-md p-2">
            <div class="flex items-center flex-wrap gap-2">
              <span class="font-medium">{{ e.title || e.id }}</span>
              <span v-if="e.type" class="text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-600">{{ e.type }}</span>
            </div>
            <div v-if="e.description" class="text-[11px] text-gray-600 mt-0.5">
              {{ (e.description || '').slice(0, 200) }}<span v-if="(e.description || '').length > 200">…</span>
            </div>
            <div v-if="e.ds_names?.length" class="mt-1 flex flex-wrap gap-2">
              <div class="flex items-center gap-1">
                <span class="text-[11px] uppercase tracking-wide text-gray-500">ds:</span>
                <div class="flex items-center gap-1">
                  <span v-for="d in e.ds_names" :key="d" class="text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-600">{{ d }}</span>
                </div>
              </div>
            </div>
            <div v-if="e.code" class="mt-2">
              <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Code</div>
              <pre class="text-[11px] text-gray-900 whitespace-pre-wrap bg-gray-50 rounded p-2 overflow-x-auto">{{ (e.code || '').slice(0, 2000) }}</pre>
            </div>
            <div v-if="e.data_model" class="mt-2">
              <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Data Model</div>
              <pre class="text-[11px] text-gray-900 whitespace-pre-wrap bg-gray-50 rounded p-2 overflow-x-auto">{{ formatJson(e.data_model) }}</pre>
            </div>
          </div>
        </div>
      </Transition>
    </div>

    <!-- Metadata Section (generalized key-value) -->
    <div v-if="metadata && Object.keys(metadata).length > 0">
      <div 
        class="flex items-center cursor-pointer text-[11px] uppercase tracking-wide text-gray-500 mb-2"
        @click="toggleSection('metadata')"
      >
        <Icon :name="expandedSections.has('metadata') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1" />
        Metadata
      </div>
      <Transition name="fade">
        <div v-if="expandedSections.has('metadata')" class="ml-4">
          <div v-if="plannerPromptTokens !== null" class="flex items-baseline text-xs py-0.5 mb-2">
            <div class="w-44 text-[11px] uppercase tracking-wide text-gray-500">Planner Prompt Tokens</div>
            <div class="text-gray-900 font-semibold">{{ formatNumber(plannerPromptTokens) }}</div>
          </div>
          <div v-for="[k, v] in metadataEntries" :key="k" class="text-xs py-0.5">
            <div v-if="isPrimitive(v)" class="flex items-baseline">
              <div class="w-44 text-[11px] uppercase tracking-wide text-gray-500">{{ k.replace(/_/g,' ') }}</div>
              <div class="text-gray-900">{{ formatPrimitive(v) }}</div>
            </div>
            <div v-else>
              <div class="flex items-center cursor-pointer" @click="toggleSection('meta:'+k)">
                <Icon :name="expandedSections.has('meta:'+k) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 mr-1 text-gray-500" />
                <div class="w-44 text-[11px] uppercase tracking-wide text-gray-500">{{ k.replace(/_/g,' ') }}</div>
              </div>
              <Transition name="fade">
                <div v-if="expandedSections.has('meta:'+k)" class="ml-4">
                  <pre class="text-xs text-gray-700 whitespace-pre-wrap">{{ JSON.stringify(v, null, 2) }}</pre>
                </div>
              </Transition>
            </div>
          </div>
        </div>
      </Transition>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'

interface Props {
  contextData: any
}

const props = defineProps<Props>()

const expandedSections = ref<Set<string>>(new Set())
// Object-based schemas (preferred), with fallback to legacy text
const objectSchemas = computed(() => props.contextData?.static?.schemas || null)
const dataSources = computed(() => {
  const ds = objectSchemas.value?.data_sources || []
  return Array.isArray(ds) ? ds : []
})
const schemasText = computed(() => {
  const s = props.contextData?.static?.schemas || props.contextData?.schemas_excerpt || ''
  return typeof s === 'string' ? s : ''
})

// Parse XML-like tags: <tag>: ... </tag> or <tag>...</tag>
const xmlSections = computed(() => {
  const text = schemasText.value
  if (!text) return [] as Array<{ tag: string, content: string }>
  const sections: Array<{ tag: string, content: string }> = []
  const re = /<([a-zA-Z_][\w-]*)>:?[\s\S]*?<\/\1>/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const full = m[0]
    const tag = m[1]
    // Extract inner content (between the tags)
    const inner = full.replace(new RegExp(`^<${tag}>:?`), '').replace(new RegExp(`<\/${tag}>$`), '')
    sections.push({ tag, content: inner.trim() })
  }
  return sections
})

// Parse CREATE TABLE blocks from the <schema> tag
const tables = computed(() => {
  const schema = xmlSections.value.find(s => s.tag === 'schema')
  const sql = schema?.content || ''
  if (!sql) return [] as Array<any>
  const results: any[] = []
  const re = /CREATE TABLE\s+([^\s(]+)\s*\(([\s\S]*?)\)\s*/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(sql)) !== null) {
    const name = (m[1] || '').trim()
    const body = (m[2] || '').trim()
    const { columns, metrics } = parseTableBody(body)
    results.push({ name, columns, metrics })
  }
  return results
})

// Instructions (object-first)
const instructionsItems = computed(() => props.contextData?.static?.instructions?.items || [])
const instructionsText = computed(() => props.contextData?.static?.instructions || props.contextData?.instructions_context || '')

const instructionsList = computed(() => {
  const text = instructionsText.value || ''
  if (!text) return [] as Array<{ key: string, id?: string, category?: string, content: string }>
  const list: Array<{ key: string, id?: string, category?: string, content: string }> = []
  const re = /<instruction([^>]*)>([\s\S]*?)<\/instruction>/gi
  let m: RegExpExecArray | null
  let idx = 0
  while ((m = re.exec(text)) !== null) {
    const attrStr = (m[1] || '').trim()
    const content = (m[2] || '').trim()
    const attrs: Record<string, string> = {}
    const reAttr = /(\w+)="([^"]*)"/g
    let a: RegExpExecArray | null
    while ((a = reAttr.exec(attrStr)) !== null) {
      attrs[a[1]] = a[2]
    }
    list.push({ key: attrs.id || String(idx++), id: attrs.id, category: attrs.category, content })
  }
  return list
})

const resourcesContent = computed(() => props.contextData?.static?.resources?.content || '')
const metadataResources = computed<any[]>(() => [])

const observations = computed(() => props.contextData?.warm?.observations || null)

// Parsed observations data
const toolObservations = computed(() => {
  const obs = observations.value
  if (!obs) return []
  // Handle both object and array formats
  if (Array.isArray(obs.tool_observations)) {
    return obs.tool_observations
  }
  if (Array.isArray(obs)) {
    return obs
  }
  return []
})

const widgetUpdates = computed(() => {
  const obs = observations.value
  if (!obs || !Array.isArray(obs.widget_updates)) return []
  return obs.widget_updates
})

const visualizationUpdates = computed(() => {
  const obs = observations.value
  if (!obs || !Array.isArray(obs.visualization_updates)) return []
  return obs.visualization_updates
})

const mentions = computed(() => props.contextData?.warm?.mentions || null)

const entities = computed(() => props.contextData?.warm?.entities || null)

const metadata = computed(() => {
  const meta = props.contextData?.meta || {}
  // Filter out large or uninteresting metadata
  const filtered: any = {}
  for (const [key, value] of Object.entries(meta)) {
    if (
      key !== 'context_view_json' &&
      key !== 'context_window_start' &&
      key !== 'context_window_end' &&
      key !== 'memories_count' &&
      key !== 'metadata_resources_count' &&
      typeof value !== 'function'
    ) {
      filtered[key] = value
    }
  }
  return filtered
})

const metadataEntries = computed(() => Object.entries(metadata.value || {}))
const plannerPromptTokens = computed(() => {
  const meta = metadata.value || {}
  const sectionSizes = meta.section_sizes || {}
  if (typeof sectionSizes?._planner_prompt_total === 'number') {
    return sectionSizes._planner_prompt_total
  }
  if (typeof meta.total_tokens === 'number') {
    return meta.total_tokens
  }
  return null
})

function toggleSection(section: string) {
  if (expandedSections.value.has(section)) {
    expandedSections.value.delete(section)
  } else {
    expandedSections.value.add(section)
  }
}

// Open schemas and data sources by default
onMounted(() => {
  //expandedSections.value.add('schemas')
})

watch(dataSources, (list) => {
  for (const ds of list) {
    const id = ds?.info?.id
    if (id) expandedSections.value.add('ds:' + id)
  }
}, { immediate: true })

// Helpers
function parseTableBody(body: string): { columns: Array<{ name: string, type: string }>, metrics: Record<string, any> } {
  const columns: Array<{ name: string, type: string }> = []
  const metrics: Record<string, any> = {}

  const lines = body.split(/\n+/)
  for (const raw of lines) {
    const line = raw.trim().replace(/,+$/,'')
    if (!line) continue
    // Metrics detection
    if (line.startsWith('--')) {
      // look ahead for key: value pairs on same or following lines
      extractMetricsFromLine(line, metrics)
      continue
    }
    // Also capture plain metric lines like "score: 0.0"
    if (/^(score|usage|success|failure|success_rate|feedback)\s*:/i.test(line)) {
      extractMetricsFromLine(line, metrics)
      continue
    }
    // Column pattern: name type [extras]
    const match = line.match(/^([^\s]+)\s+(.+)$/)
    if (match) {
      const colName = match[1]
      const colType = match[2].replace(/,$/, '').trim()
      // ignore lines that are clearly not columns
      if (!/^(--|score:|usage:|success:|failure:|success_rate:|feedback:)/i.test(colName)) {
        columns.push({ name: colName, type: colType })
      }
    }
  }
  return { columns, metrics }
}

function extractMetricsFromLine(line: string, metrics: Record<string, any>) {
  // Normalize commas separation: "-- metrics --, key: val, key2: val2"
  const parts = line.replace(/^--.*?--\s*,?/,'').split(',')
  for (const part of parts) {
    const p = part.trim()
    if (!p) continue
    // feedback: +0 / -1 pattern
    const fb = p.match(/^feedback\s*:\s*([+\-]?\d+)\s*\/\s*([+\-]?\d+)/i)
    if (fb) {
      metrics['feedback_positive'] = Number(fb[1])
      metrics['feedback_negative'] = Number(fb[2])
      continue
    }
    const kv = p.match(/^([a-zA-Z_][\w]*)\s*:\s*([-+]?\d+(?:\.\d+)?)/)
    if (kv) {
      metrics[kv[1]] = Number(kv[2])
    }
  }
}

// Formatting helpers for metrics display
function formatMetricsList(metrics: Record<string, any>): Array<{ key: string, label: string, value: any, class: string }> {
  const entries: Array<{ key: string, label: string, value: any, class: string }> = []
  const order = ['score', 'usage', 'success', 'failure', 'success_rate', 'feedback_positive', 'feedback_negative']
  const cls = (k: string): string => {
    if (k === 'score') return 'text-gray-700'
    if (k === 'success') return 'text-green-600'
    if (k === 'failure' || k === 'feedback_negative') return 'text-red-600'
    if (k === 'success_rate') return 'text-blue-600'
    if (k === 'usage' || k === 'feedback_positive') return 'text-gray-600'
    return 'text-gray-700'
  }
  for (const key of order) {
    if (metrics[key] !== undefined) {
      entries.push({ key, label: key.replace(/_/g, ' '), value: metrics[key], class: cls(key) })
    }
  }
  // Append any other keys
  for (const [k, v] of Object.entries(metrics)) {
    if (!order.includes(k)) entries.push({ key: k, label: k.replace(/_/g, ' '), value: v, class: 'text-gray-700' })
  }
  return entries
}

function formatMetricsBadges(metrics: Record<string, any>): Array<{ key: string, label: string, value: any, class: string }> {
  const base = formatMetricsList(metrics)
  // Convert class variants to bordered badge styles
  return base.map(m => ({
    ...m,
    class: m.class.replace('text-', 'border-').replace('600', '300') + ' ' + m.class
  }))
}

// Score helpers for headline dot and formatting
function scoreDotClass(score: number | undefined): string {
  if (score === undefined || score === null) return 'bg-gray-300'
  if (score > 0) return 'bg-green-200'
  if (score < 0) return 'bg-red-200'
  return 'bg-gray-300'
}

function formatScore(score: number | undefined): string {
  if (score === undefined || score === null) return '0'
  const n = Number(score)
  if (Number.isNaN(n)) return String(score)
  return n % 1 === 0 ? String(n) : n.toFixed(2)
}

// Metadata helpers
function isPrimitive(v: any): boolean {
  return v === null || ['string', 'number', 'boolean'].includes(typeof v)
}

function formatPrimitive(v: any): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'number') return formatNumber(v)
  return String(v)
}

function formatNumber(value: number | null): string {
  if (value === null || value === undefined) return '—'
  try {
    return value.toLocaleString()
  } catch {
    return String(value)
  }
}

function formatJson(v: any): string {
  try {
    return typeof v === 'string' ? v : JSON.stringify(v, null, 2)
  } catch (e) {
    return String(v)
  }
}

// Observation helpers
function getObservationHeaderClass(obs: any): string {
  if (hasObservationError(obs)) {
    return 'bg-red-50'
  }
  return 'bg-gray-50'
}

function hasObservationError(obs: any): boolean {
  if (!obs?.observation) return false
  return !!obs.observation.error || obs.observation.summary?.toLowerCase().includes('error') || obs.observation.summary?.toLowerCase().includes('failed')
}

function hasObservationSuccess(obs: any): boolean {
  if (!obs?.observation) return false
  if (hasObservationError(obs)) return false
  return !!obs.observation.summary
}

function formatObservationTime(timestamp: string): string {
  if (!timestamp) return ''
  try {
    const date = new Date(timestamp)
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return timestamp
  }
}

function truncateText(text: string, maxLength: number): string {
  if (!text) return ''
  if (text.length <= maxLength) return text
  return text.slice(0, maxLength) + '…'
}

function getOtherInputFields(input: any): Array<[string, any]> {
  if (!input || typeof input !== 'object') return []
  const skipKeys = ['title', 'user_prompt', 'interpreted_prompt', 'tables_by_source']
  return Object.entries(input).filter(([k]) => !skipKeys.includes(k))
}

function formatInputValue(value: any): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return truncateText(value, 100)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return `[${value.length} items]`
  if (typeof value === 'object') return '{...}'
  return String(value)
}

function getOtherObservationFields(observation: any): Array<[string, any]> {
  if (!observation || typeof observation !== 'object') return []
  const skipKeys = ['summary', 'error']
  return Object.entries(observation).filter(([k, v]) => !skipKeys.includes(k) && v !== null && v !== undefined)
}

// Build metrics map for object-based table entry
function tableMetrics(tbl: any): Array<{ key: string, label: string, value: any }> {
  if (!tbl || typeof tbl !== 'object') return []
  const keys = [
    'usage_count',
    'success_count',
    'failure_count',
    'weighted_usage_count',
    'pos_feedback_count',
    'neg_feedback_count',
    'last_used_at',
    'last_feedback_at',
    'success_rate',
    'score',
  ]
  const items: Array<{ key: string, label: string, value: any }> = []
  for (const k of keys) {
    const v = (tbl as any)[k]
    if (v !== undefined && v !== null) items.push({ key: k, label: k.replace(/_/g, ' '), value: v })
  }
  return items
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>