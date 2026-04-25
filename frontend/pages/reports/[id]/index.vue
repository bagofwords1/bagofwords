<template>

	<!-- Loading until report and completions are fetched -->
	<div v-if="(!reportLoaded || !completionsLoaded) && messages.length === 0 && !reportNotFound" class="h-screen w-full flex items-center justify-center text-gray-500">
		<Spinner class="w-5 h-5 me-2" />
		<span class="text-sm">{{ $t('reportView.loadingReport') }}</span>
	</div>

	<!-- Report not found / no access -->
	<div v-else-if="reportNotFound" class="h-screen w-full flex flex-col items-center justify-center text-gray-400">
		<span class="text-5xl font-light">404</span>
		<span class="mt-2 text-sm">{{ $t('reportView.reportNotFound') }}</span>
		<NuxtLink to="/reports" class="mt-4 text-sm text-blue-500 hover:underline">{{ $t('reportView.backToReports') }}</NuxtLink>
	</div>

	<SplitScreenLayout v-else
		:isSplitScreen="isSplitScreen" 
		:leftPanelWidth="leftPanelWidth"
		:isResizing="isResizing"
		@startResize="startResize"
	>
		<template #left>
	<div class="flex flex-col h-screen overflow-y-hidden bg-white relative">
		<ReportHeader
			v-if="report"
			:report="report"
			:isSplitScreen="isSplitScreen"
			:isStreaming="isStreaming"
			:isMobile="isMobile"
			:mobileView="mobileView"
			@toggleSplitScreen="toggleSplitScreen"
			@stop="abortStream"
			@update:mobileView="(v: any) => mobileView = v"
		/>

		<!-- Mobile right panel content (full screen) -->
		<div v-if="isMobile && mobileView !== 'chat'" class="flex-1 min-h-0 overflow-hidden flex flex-col">
			<div class="flex-1 min-h-0 p-2">
				<div class="h-full w-full bg-[#f8f8f7] rounded-xl border border-black/[0.08] overflow-hidden">
					<!-- Summary View -->
					<div v-if="mobileView === 'summary'" class="h-full overflow-y-auto">
						<ChatSummary
							:scheduledPrompts="scheduledPrompts"
							:artifactList="reportArtifacts"
							:queryList="queryList"
							:queryExecutions="summaryQueries"
							:trainingInstructions="summaryInstructions"
						:pendingTrainingBuild="pendingTrainingBuild"
						:pendingTrainingBuildDiff="pendingTrainingBuildDiff"
						@approveTrainingBuild="onApproveTrainingBuild"
						@discardTrainingBuild="onDiscardTrainingBuild"
							@editScheduledPrompt="editScheduledPrompt"
							@openArtifact="handleOpenArtifact"
							@scrollToMessage="scrollToMessage"
							/>
					</div>
					<!-- Agent View -->
					<div v-else-if="mobileView === 'agent'" class="h-full overflow-y-auto">
						<ReportAgentPanel ref="mobileAgentPanelRef" :agents="currentAgents" />
					</div>
					<!-- Dashboard View -->
					<ArtifactFrame
						v-else-if="mobileView === 'dashboard' && reportLoaded && report?.id"
						:report-id="report.id"
						:report="report"
						@close="mobileView = 'chat'"
						class="h-full"
					/>
				</div>
			</div>
		</div>

		<!-- Chat content (hidden on mobile when viewing other tabs) -->
		<template v-if="!isMobile || mobileView === 'chat'">
		<!-- Fork banner -->
		<ForkBanner
			v-if="report?.forked_from_id"
			:forked-from-id="report.forked_from_id"
			:forked-from-title="report.forked_from_title"
			:forked-from-user-name="report.forked_from_user_name"
		/>

		<!-- Messages -->
		<div class="flex-1 overflow-y-auto mt-4 pb-4" :class="{ 'compact-messages': isExcel }" ref="scrollContainer">
			<div class="ps-4 pe-2 pb-[3px] max-w-2xl w-full mx-auto">

				<!-- Forked queries panel (shown for forked reports) -->
				<ForkedQueriesPanel
					v-if="forkedQueries.length > 0"
					:queries="forkedQueries"
					:artifact-ref="forkedArtifactRef"
				/>

				<!-- Fork summary separator -->
				<div v-if="report?.forked_from_id && nonSeedMessages.length > 0" class="flex items-center gap-3 my-4">
					<div class="flex-1 border-t border-dashed border-gray-200"></div>
					<span class="text-[10px] text-gray-300 uppercase tracking-wider">{{ $t('reportView.yourConversation') }}</span>
					<div class="flex-1 border-t border-dashed border-gray-200"></div>
				</div>

				<ul v-if="messages.length > 0" class="mx-auto w-full">
					<!-- Top loader for older pages -->
					<li v-if="hasMore && isLoadingMore" class="text-gray-500 mb-2 text-xs text-center">
						<Spinner class="w-4 h-4 inline me-2" /> {{ $t('reportView.loadingOlderMessages') }}
					</li>
					<li v-for="m in messages" :key="m.id" :data-message-id="m.id" class="text-gray-700 mb-2 text-sm">
						<!-- Fork summary card (special rendering) -->
						<div v-if="(m as any).is_fork_summary" class="rounded-lg border border-amber-100 bg-amber-50/50 p-3 mb-4">
							<div class="flex items-center gap-1.5 text-xs text-amber-600 mb-2">
								<Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5" />
								<span class="font-medium">{{ $t('reportView.summaryOfOriginal') }}</span>
							</div>
							<div class="text-xs text-gray-600 leading-relaxed whitespace-pre-line">{{ (m as any).completion?.content || '' }}</div>
						</div>

						<!-- Scheduled prompt: collapsible header + user bubble when expanded -->
						<div v-else-if="m.scheduled_prompt_id && m.role === 'user'">
							<button
								@click="toggleScheduledExpand(m.id)"
								class="w-full flex items-center gap-1.5 px-3 py-2 text-xs text-gray-400 rounded-lg border border-gray-100 bg-gray-50/50 hover:bg-gray-50 transition-colors mb-2"
							>
								<Icon name="heroicons-clock" class="w-3.5 h-3.5" />
								<span class="font-medium text-gray-500">{{ $t('reportView.scheduledRun') }}</span>
								<span class="text-gray-300">{{ formatScheduledDate(m.created_at) }}</span>
								<span v-if="getScheduledStats(m)" class="text-gray-300">&middot;</span>
								<span v-if="getScheduledStats(m)" class="text-gray-400">{{ getScheduledStats(m) }}</span>
								<Icon :name="isScheduledExpanded(m.id) ? 'heroicons-chevron-up' : 'heroicons-chevron-down'" class="w-3 h-3 ms-auto flex-shrink-0" />
							</button>
							<!-- User bubble shown inside the collapsible area -->
							<div v-if="isScheduledExpanded(m.id)" class="flex rounded-lg p-1 justify-end">
								<div class="flex items-start gap-2 max-w-xl w-full mb-4">
									<div class="flex-1 flex justify-end">
										<div class="inline-block rounded-xl px-3 py-2 bg-gray-50 text-gray-900 text-start" dir="auto">
											<div v-if="m.prompt?.content" class="pt-1 markdown-wrapper">
												<MDC :value="m.prompt.content" class="markdown-content" />
											</div>
										</div>
									</div>
									<div class="flex-shrink-0 hidden md:block md:w-[28px]">
										<div class="h-7 w-7 uppercase flex items-center justify-center text-xs border border-blue-200 bg-blue-100 rounded-full inline-block">
											{{ report.user.name.charAt(0) }}
										</div>
									</div>
								</div>
							</div>
						</div>

						<!-- Scheduled system message: hide when user header is collapsed -->
						<template v-else-if="m.scheduled_prompt_id && m.role === 'system' && !isScheduledSystemExpanded(m)">
							<!-- collapsed -->
						</template>

						<!-- Regular message rendering -->
						<div
							v-else
							class="flex rounded-lg p-1"
							:class="m.role === 'user' ? 'justify-end' : 'justify-start'"
						>
							<!-- User message (start-edge bubble; flips to opposite edge under RTL via ul dir) -->
							<template v-if="m.role === 'user'">
								<div class="flex items-start gap-2 max-w-xl w-full mb-4">
									<!-- User message bubble -->
									<div class="flex-1 flex justify-end">
										<div class="inline-block rounded-xl px-3 py-2 bg-gray-50 text-gray-900 text-start " dir="auto">
											<div v-if="m.prompt?.content" class="pt-1 markdown-wrapper">
												<MDC :value="m.prompt.content" class="markdown-content" />
											</div>
											<!-- Attached images thumbnail -->
											<div v-if="getAttachedImages(m).length > 0" class="mt-2 flex flex-wrap gap-1.5">
												<div v-for="file in getAttachedImages(m)" :key="file.id" class="relative group">
													<AuthenticatedImage
														:file-id="file.id"
														:alt="file.filename"
														img-class="h-16 w-16 object-cover rounded-lg border border-gray-200 cursor-pointer hover:opacity-90 transition-opacity"
														@click="openImagePreview(file)" />
												</div>
											</div>
										</div>
									</div>
									<!-- User avatar on the right (hidden on mobile) -->
									<div class="flex-shrink-0 hidden md:block md:w-[28px]">
										<div class="h-7 w-7 uppercase flex items-center justify-center text-xs border border-blue-200 bg-blue-100 rounded-full inline-block">
											{{ report.user.name.charAt(0) }}
										</div>
									</div>
								</div>
							</template>

							<!-- System / assistant message (left-aligned, keep existing styling) -->
							<template v-else>
								<!-- AI avatar (hidden on mobile) -->
								<div class="w-[28px] me-2 flex-shrink-0 hidden md:block">
									<div class="h-7 w-7 flex font-bold items-center justify-center text-xs rounded-lg inline-block bg-contain bg-center bg-no-repeat" style="background-image: url('/assets/logo-128.png')">
									</div>
								</div>
								<div class="w-full ms-4 max-w-2xl">
									<!-- System message -->
									<div>
										<!-- Render each completion block - unified structure -->
										<div v-for="(block, blockIndex) in (m.completion_blocks || []).filter(b => b.phase !== 'knowledge_harness')" :key="block.id">
											<!-- 1. Thinking box (reasoning only) -->
											<div v-if="block.plan_decision?.reasoning || block.reasoning || block.status === 'stopped'" class="thinking-box">
												<div class="thinking-header" @click="toggleReasoning(block.id)">
													<Icon :name="isReasoningCollapsed(block.id) ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-4 h-4 text-gray-400 rtl-flip" />
													<span v-if="hasCompletedContent(block) || block.tool_execution" class="ms-1">
														{{ getThoughtProcessLabel(block) }}
													</span>
													<span v-else class="ms-1">
														<div class="dots" />
													</span>
												</div>
												<Transition name="fade">
													<div 
														v-if="!isReasoningCollapsed(block.id)" 
														:ref="el => setReasoningRef(block.id, el)"
														class="thinking-content"
													>
														<template v-if="block.plan_decision?.reasoning || block.reasoning">
															<MarkdownRender
																:content="block.plan_decision?.reasoning || block.reasoning || ''"
																:final="isBlockFinalized(block)"
																:typewriter="!isBlockFinalized(block)"
																:render-code-blocks-as-pre="true"
																class="markdown-content"
															/>
														</template>
														<template v-else-if="block.status === 'stopped'">
															<div class="text-gray-400 italic">{{ $t('reportView.generationStoppedBefore') }}</div>
														</template>
													</div>
												</Transition>
											</div>

							<!-- 2. Block content - assistant message (hybrid streaming) -->
							<!-- Prioritize final_answer over assistant - final_answer is the actual response -->
							<!-- Show content section when: content exists OR final_answer exists OR assistant exists -->
							<div v-if="(block.content || block.plan_decision?.final_answer || block.plan_decision?.assistant) && block.status !== 'error'" class="block-content markdown-wrapper" dir="auto">
								<MarkdownRender
									:content="block.content || block.plan_decision?.final_answer || block.plan_decision?.assistant || ''"
									:final="isBlockFinalized(block)"
									:typewriter="!isBlockFinalized(block)"
									:render-code-blocks-as-pre="true"
									class="markdown-content"
								/>
											</div>

											<!-- 3. Tool execution (ALWAYS visible outside thinking) -->
											<div v-if="block.tool_execution" class="tool-execution-container" :data-step-id="block.tool_execution?.created_step?.id || block.tool_execution?.created_step_id || ''">
												<component
													v-if="shouldUseToolComponent(block.tool_execution)"
													:is="getToolComponent(block.tool_execution.tool_name)"
													:key="`${block.id}:${(block.tool_execution && block.tool_execution.id) ? block.tool_execution.id : 'noid'}`"
													:tool-execution="block.tool_execution"
													:data-sources="report?.data_sources"
													@addWidget="handleAddWidgetFromPreview"
													@refreshDashboard="refreshDashboardFast"
													@toggleSplitScreen="toggleSplitScreen"
													@editQuery="handleEditQuery"
													@openArtifact="handleOpenArtifact"
													@openInstruction="openInstructionById"
												/>
												<!-- Fallback to generic expandable tool display -->
												<div v-else>
													<div class="text-xs text-gray-500 mb-1">
														<span class="cursor-pointer hover:text-gray-700" @click="toggleToolDetails(block.tool_execution.id)" v-if="block.tool_execution.tool_name !== 'clarify' && block.tool_execution.tool_name !== 'answer_question' && block.tool_execution.tool_name !== 'suggest_instructions'">
															{{ block.tool_execution.tool_name }}{{ block.tool_execution.tool_action ? ` → ${block.tool_execution.tool_action}` : '' }} ({{ block.tool_execution.status }})
														</span>
														<div v-if="isToolDetailsExpanded(block.tool_execution.id)" class="ms-2 mt-1 text-xs text-gray-400 bg-gray-50 p-2 rounded">
															<div v-if="block.tool_execution.result_summary">{{ block.tool_execution.result_summary }}</div>
															<div v-if="block.tool_execution.duration_ms">{{ $t('reportView.duration', { ms: block.tool_execution.duration_ms }) }}</div>
															<div v-if="block.tool_execution.created_widget_id" class="text-green-600">{{ $t('reportView.widgetRef', { id: block.tool_execution.created_widget_id }) }}</div>
															<div v-if="block.tool_execution.created_step_id" class="text-purple-600">{{ $t('reportView.stepRef', { id: block.tool_execution.created_step_id }) }}</div>
														</div>
													</div>
												</div>
											</div>
											
											<!-- Tool widget preview -->
											<div class="mt-1" v-if="shouldShowToolWidgetPreview(block.tool_execution) && block.tool_execution">
												<ToolWidgetPreview :tool-execution="block.tool_execution" @addWidget="handleAddWidgetFromPreview" @toggleSplitScreen="toggleSplitScreen" @editQuery="handleEditQuery" />
											</div>

																	</div>

										<!-- Knowledge group: harness-phase blocks rendered as a single collapsible card -->
										<KnowledgeGroup
											v-if="(m as any)._harness_running || (m.completion_blocks || []).some(b => (b as any).phase === 'knowledge_harness')"
											:blocks="((m.completion_blocks || []).filter(b => (b as any).phase === 'knowledge_harness') as any)"
											:harness-running="!!(m as any)._harness_running"
											:knowledge-harness-build="(m as any).knowledge_harness_build || null"
											@open-instruction="openInstructionById"
											@published="() => loadCompletions({ skipEstimate: true })"
										/>

										<!-- Thinking dots when system is working but no visible progress - moved to end -->
										<div v-if="shouldShowWorkingDots(m)" class="mt-2">
											<div class="simple-dots"></div>
										</div>
									</div>

									<!-- Show status messages for stopped/error completions -->
									<div class="mt-2" v-if="isRealCompletion(m) && m.status === 'success'">
										<div class="flex items-center gap-1">
											<CompletionItemFeedback
												:completion="{ id: (m.system_completion_id || m.id) }"
												:feedbackScore="m.feedback_score || 0"
												:initialUserFeedback="m.user_feedback"
												@suggestionsLoading="() => handleSuggestionsLoading(m)"
												@suggestionsReceived="(suggestions) => handleSuggestionsReceived(m, suggestions)"
											/>

											<!-- Instructions loaded indicator with popover -->
											<UPopover v-if="visibleInstructions(m).length" :popper="{ placement: 'top-start' }" ref="instructionsPopoverRef">
												<UButton variant="ghost" color="gray" size="xs" class="!px-1.5">
													<Icon name="heroicons-cube" class="w-3.5 h-3.5" />
													<span class="text-xs text-gray-700 font-normal">{{ $t('reportView.instructionsCount', { count: visibleInstructions(m).length }) }}</span>
												</UButton>
												<template #panel="{ close }">
													<div class="p-3 w-[380px] max-h-[300px] overflow-y-auto">
														<div class="text-[11px] uppercase tracking-wide text-gray-400 mb-2">{{ $t('reportView.instructionsLoaded') }}</div>
														<div class="space-y-0.5">
															<div
																v-for="ins in visibleInstructions(m)"
																:key="ins.id"
																class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 cursor-pointer text-xs text-gray-700"
																@click="close(); openInstructionById(ins.id)"
															>
																<DataSourceIcon v-if="ins.data_source_type" :type="ins.data_source_type" class="h-3.5 w-3.5 flex-shrink-0" />
																<Icon v-else name="heroicons-cube" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
																<span class="flex-1 truncate">{{ ins.title || $t('reportView.untitled') }}</span>
																<span class="text-[10px] text-gray-400 flex-shrink-0">{{ ins.category || 'general' }}</span>
																<span class="text-[9px] px-1.5 py-0.5 rounded flex-shrink-0"
																	:class="(ins.load_mode || 'always') === 'always' ? 'bg-green-50 text-green-600' : 'bg-blue-50 text-blue-600'">
																	{{ ins.load_mode || 'always' }}
																</span>
															</div>
														</div>
													</div>
												</template>
											</UPopover>

											<!-- Debug button -->
											<button
												v-if="canViewConsole"
												@click="openTraceModal(m.system_completion_id || m.id)"
												class="flex items-center justify-center w-6 h-6 hover:bg-gray-50 rounded-md transition-colors group"
												:title="$t('reportView.viewAgentTrace')"
											>
												<Icon name="heroicons-bug-ant" class="w-4 h-4 text-gray-500 group-hover:text-gray-900" />
											</button>
										</div>
									</div>

									<!-- Instruction Suggestions (below thumbs) - show when loading or has suggestions -->
									<div v-if="report?.mode !== 'training' && !((m.completion_blocks || []).some(b => (b as any).phase === 'knowledge_harness')) && ((m.instruction_suggestions && m.instruction_suggestions.length > 0) || m.instruction_suggestions_loading)" class="mt-3">
										<InstructionSuggestions
											:tool-execution="{
												id: `suggestions-${m.id}`,
												tool_name: 'suggest_instructions',
												status: m.instruction_suggestions_loading ? 'running' : 'success',
												result_json: { drafts: m.instruction_suggestions || [] }
											}"
										/>
									</div>
									<div v-if="m.status === 'stopped'" class="text-xs text-gray-500 mt-2 italic">
										<Icon name="heroicons-stop-circle" class="w-4 h-4 inline me-1" />
										Generation stopped
									</div>
									<div v-else-if="m.status === 'error'" class="text-xs text-gray-500">
										<Icon name="heroicons-x-mark" class="w-4 h-4 inline me-1 text-red-500" />
										<span v-if="getMessageError(m)" class="pre-wrap">
											<Icon name="heroicons-x-mark" class="w-4 h-4 inline me-1 text-red-500" />
											{{ getMessageError(m) }}</span>
										<span v-else class="italic">An error occurred</span>
									</div>
								</div>
							</template>
						</div>
					</li>
			</ul>
			<div v-else class="mt-32 fade-in">
				<h1 class="text-4xl mb-4">🪴</h1>
				<h1 class="text-lg font-semibold">{{ $t('reports.emptyTitle') }}</h1>

				<hr class="my-4">
				<p class="text-gray-500 text-sm"><span class="font-semibold">{{ $t('reports.emptyTipLabel') }}</span> <br />
					{{ $t('reports.emptyTipBody') }}
				</p>

			</div>
			</div>
		</div>

		<!-- Minimal reconnect banner while polling after refresh (bottom, above prompt) -->
		<div v-if="isPolling" class="mx-auto px-4 mt-2 mb-2 max-w-2xl w-full">
			<div class="text-xs text-gray-500 flex items-center">
				<Spinner class="w-3 h-3 me-2 text-gray-400" />
				<span class="poll-shimmer">Loading… showing recent progress</span>
			</div>
		</div>
		<div v-if="report.report_type === 'test'" class="mx-auto px-4 mt-2 mb-2 max-w-2xl w-full">
			<div class="text-xs text-gray-500 flex items-center">
				<span class="text-xs">
					<span class="font-medium bg-yellow-100 text-yellow-800 px-2 py-1 rounded-md">Note
						This report is a report generated from a test run
					</span>
					</span>
				</div>
			</div>
		<div v-if="report.external_platform?.platform_type === 'mcp'" class="mx-auto px-4 mt-2 mb-2 max-w-2xl w-full">
			<div class="text-xs flex items-center">
				<span class="font-medium bg-blue-50 text-blue-700 px-3 py-2 rounded-md flex items-center gap-2">
					<img src="/icons/mcp.png" class="h-4 w-4" />
					<span>This session was created via MCP. The conversation reflects tool calls made by an external AI assistant. You can view the generated data and visualizations above.</span>
				</span>
			</div>
		</div>
		<div v-if="report.external_platform?.platform_type === 'slack'" class="mx-auto px-4 mt-2 mb-2 max-w-2xl w-full">
			<div class="text-xs flex items-center">
				<span class="font-medium bg-blue-50 text-blue-700 px-3 py-2 rounded-md flex items-center gap-2">
					<img src="/icons/slack.png" class="h-4 w-4" />
					<span>This session was created via Slack.</span>
				</span>
			</div>
		</div>
		<div v-if="report.external_platform?.platform_type === 'teams'" class="mx-auto px-4 mt-2 mb-2 max-w-2xl w-full">
			<div class="text-xs flex items-center">
				<span class="font-medium bg-blue-50 text-blue-700 px-3 py-2 rounded-md flex items-center gap-2">
					<img src="/icons/teams.png" class="h-4 w-4" />
					<span>This session was created via Microsoft Teams.</span>
				</span>
			</div>
		</div>
		<div v-if="report.external_platform?.platform_type === 'excel' && !isExcel" class="mx-auto px-4 mt-2 mb-2 max-w-2xl w-full">
			<div class="text-xs flex items-center">
				<span class="font-medium bg-green-50 text-green-700 px-3 py-2 rounded-md flex items-center gap-2">
					<img src="/data_sources_icons/excel.png" class="h-4 w-4" />
					<span>This session was created via Excel.</span>
				</span>
			</div>
		</div>
		<!-- Prompt box (in normal flow at the bottom of the left column) -->
		<div class="shrink-0 bg-white">
			<div :class="['mx-auto w-full', isExcel ? 'px-0' : 'px-4 max-w-2xl']">
				<PromptBoxV2
					ref="promptBoxRef"
					:report_id="report_id"
					:initialSelectedDataSources="report?.data_sources || []"
					:initialMode="report?.mode || 'chat'"
					:latestInProgressCompletion="isCompletionInProgress ? {} : undefined"
					:isStopping="false"
					:queryList="queryList"
					:scheduledPrompts="scheduledPrompts"
					:trainingInstructions="summaryInstructions"
						:pendingTrainingBuild="pendingTrainingBuild"
						:pendingTrainingBuildDiff="pendingTrainingBuildDiff"
						@approveTrainingBuild="onApproveTrainingBuild"
						@discardTrainingBuild="onDiscardTrainingBuild"
					:hasArtifacts="hasArtifacts"
					:compact="isExcel"
					@submitCompletion="onSubmitCompletion"
					@stopGeneration="abortStream"
					@viewDashboard="() => { if (!isSplitScreen) toggleSplitScreen(); rightPanelView = 'artifact'; }"
					@scrollToMessage="scrollToMessage"
					@editScheduledPrompt="editScheduledPrompt"
					@editTrainingInstruction="editTrainingInstruction"
					@openInstructions="() => { if (isMobile) { mobileView = 'agent'; } else { if (!isSplitScreen) toggleSplitScreen(); rightPanelView = 'agent'; } }"
					@update:selectedDataSources="(val: any[]) => currentAgents = val"
					@deleteScheduledPrompt="deleteScheduledPrompt"
					@toggleScheduledPrompt="toggleScheduledPromptActive"
					@scheduledPromptSaved="loadScheduledPrompts"
					:showContextIndicator="showContextIndicator"
				/>
			</div>
		</div>
		</template>
		<!-- Training instruction edit modal -->
		<InstructionModalComponent
			v-model="showTrainingInstructionModal"
			:instruction="editingTrainingInstruction"
			:initial-type="'global'"
			@instruction-saved="showTrainingInstructionModal = false"
		/>
		<!-- Edit scheduled prompt modal -->
		<ScheduledPromptModal
			v-model="showEditScheduledPromptModal"
			:reportId="report_id"
			:scheduledPrompt="editingScheduledPrompt"
			:initialDataSources="report?.data_sources || []"
			@saved="loadScheduledPrompts"
		/>
	</div>
		</template>
		<template #right-header>
			<div class="flex items-center gap-1">
				<button
					@click="rightPanelView = 'summary'"
					class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors"
					:class="rightPanelView === 'summary'
						? 'text-gray-900 bg-gray-100'
						: 'text-gray-400 hover:text-gray-600'"
				>
					<Icon name="heroicons:queue-list" class="w-3.5 h-3.5" />
					{{ $t('reportView.tabSummary') }}
				</button>
				<button
					@click="rightPanelView = 'artifact'"
					class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors"
					:class="rightPanelView === 'artifact' || rightPanelView === 'grid'
						? 'text-gray-900 bg-gray-100'
						: 'text-gray-400 hover:text-gray-600'"
				>
					<Icon name="heroicons:chart-bar-square" class="w-3.5 h-3.5" />
					{{ $t('reportView.tabDashboard') }}
				</button>
				<button
					@click="rightPanelView = 'agent'"
					class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors"
					:class="rightPanelView === 'agent'
						? 'text-gray-900 bg-gray-100'
						: 'text-gray-400 hover:text-gray-600'"
				>
					<DataSourceIcon
						v-if="currentAgents.length === 1"
						:type="currentAgents[0].type || currentAgents[0].connections?.[0]?.type"
						class="h-3.5 flex-shrink-0"
					/>
					<Icon v-else name="heroicons:cog-6-tooth" class="w-3.5 h-3.5" />
					{{ currentAgents.length > 1 ? $t('reportView.tabAgents') : (currentAgents[0]?.name || $t('reportView.tabAgent')) }}
				</button>
			</div>
			<button
				@click="toggleSplitScreen"
				class="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
			>
				<Icon name="heroicons:x-mark" class="w-4 h-4" />
			</button>
		</template>
		<template #right>
			<!-- Summary View -->
			<div v-if="rightPanelView === 'summary'" class="h-full flex flex-col">
				<div class="flex-1 overflow-y-auto">
					<ChatSummary
						:scheduledPrompts="scheduledPrompts"
						:artifactList="reportArtifacts"
						:queryList="queryList"
						:queryExecutions="summaryQueries"
						:trainingInstructions="summaryInstructions"
						:pendingTrainingBuild="pendingTrainingBuild"
						:pendingTrainingBuildDiff="pendingTrainingBuildDiff"
						@approveTrainingBuild="onApproveTrainingBuild"
						@discardTrainingBuild="onDiscardTrainingBuild"
						:showClose="true"
						@close="toggleSplitScreen"
						@editScheduledPrompt="editScheduledPrompt"
						@openArtifact="handleOpenArtifact"
						@scrollToMessage="scrollToMessage"
					/>
				</div>
			</div>

			<!-- Agent View -->
			<div v-else-if="rightPanelView === 'agent'" class="h-full flex flex-col">
				<div class="flex-1 overflow-y-auto">
					<ReportAgentPanel ref="agentPanelRef" :agents="currentAgents" :showClose="true" @close="toggleSplitScreen" />
				</div>
			</div>

			<!-- Grid View (DashboardComponent - Edit Mode) -->
			<DashboardComponent
				v-else-if="rightPanelView === 'grid' && reportLoaded && (visualizations || []).length >= 0"
				ref="dashboardRef"
				:report="report"
				:edit="true"
				:visualizations="visualizations"
				:textWidgetsIds="textWidgetsIds"
				:isStreaming="isStreaming"
				@toggleSplitScreen="toggleSplitScreen"
				@editVisualization="handleEditQuery"
				@toggleArtifactView="rightPanelView = 'artifact'"
				class="h-full"
			/>

			<!-- Legacy Dashboard View (reports with dashboard_layout_versions but no artifacts) -->
			<DashboardComponent
				v-else-if="rightPanelView === 'artifact' && reportLoaded && hasLegacyLayout && !hasArtifacts"
				ref="dashboardRef"
				:report="report"
				:edit="true"
				:visualizations="visualizations"
				:textWidgetsIds="textWidgetsIds"
				:isStreaming="isStreaming"
				:hideArtifactSwitch="true"
				@toggleSplitScreen="toggleSplitScreen"
				@editVisualization="handleEditQuery"
				class="h-full"
			/>

			<!-- Artifact View (handles all states: loading, empty, has artifacts) -->
			<ArtifactFrame
				v-else-if="rightPanelView === 'artifact' && reportLoaded && report?.id && !hasLegacyLayout"
				:report-id="report.id"
				:report="report"
				@close="toggleSplitScreen"
				class="h-full"
			/>

			<!-- Empty state for grid view -->
			<div v-else-if="rightPanelView === 'grid' && reportLoaded && !(visualizations || []).length" class="p-4 text-center text-gray-500 h-full">
				No dashboard items yet.
			</div>
		</template>
	</SplitScreenLayout>

	<!-- Trace Modal -->
	<TraceModal
		v-model="showTraceModal"
		:report-id="report_id"
		:completion-id="selectedCompletionForTrace || ''"
		@openInstruction="openInstructionById"
	/>

	<!-- Query Code Editor Modal -->
	<QueryCodeEditorModal
		:visible="showQueryEditor"
		:query-id="queryEditorProps.queryId"
		:step-id="queryEditorProps.stepId"
		:initial-code="queryEditorProps.initialCode"
		:title="queryEditorProps.title"
		@close="closeQueryEditor"
		@stepCreated="onStepCreated"
	/>

	<!-- Image Preview Modal -->
	<ImagePreviewModal ref="imagePreviewModalRef" />

</template>

<script setup lang="ts">
import { ref, nextTick, onMounted, onUnmounted, watch, computed, type ComponentPublicInstance } from 'vue'
import PromptBoxV2 from '~/components/prompt/PromptBoxV2.vue'
import CreateWidgetTool from '~/components/tools/CreateWidgetTool.vue'
import CreateDataTool from '~/components/tools/CreateDataTool.vue'
import CreateDashboardTool from '~/components/tools/CreateDashboardTool.vue'
import CreateArtifactTool from '~/components/tools/CreateArtifactTool.vue'
import ReadArtifactTool from '~/components/tools/ReadArtifactTool.vue'
import ReadQueryTool from '~/components/tools/ReadQueryTool.vue'
import EditArtifactTool from '~/components/tools/EditArtifactTool.vue'
import AnswerQuestionTool from '~/components/tools/AnswerQuestionTool.vue'
import DescribeTablesTool from '~/components/tools/DescribeTablesTool.vue'
import DescribeEntityTool from '~/components/tools/DescribeEntityTool.vue'
import ReadResourcesTool from '~/components/tools/ReadResourcesTool.vue'
import InspectDataTool from '~/components/tools/InspectDataTool.vue'
import MCPTool from '~/components/tools/MCPTool.vue'
import WriteCsvTool from '~/components/tools/WriteCsvTool.vue'
import WriteToExcelTool from '~/components/tools/WriteToExcelTool.vue'
import WriteOfficeJsCodeTool from '~/components/tools/WriteOfficeJsCodeTool.vue'
import ReadExcelRangeTool from '~/components/tools/ReadExcelRangeTool.vue'
import ReadExcelAsCsvTool from '~/components/tools/ReadExcelAsCsvTool.vue'
import InstructionSuggestions from '@/components/InstructionSuggestions.vue'
import CreateInstructionTool from '~/components/tools/CreateInstructionTool.vue'
import EditInstructionTool from '~/components/tools/EditInstructionTool.vue'
import SearchInstructionsTool from '~/components/tools/SearchInstructionsTool.vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import ExecuteCodeTool from '~/components/tools/ExecuteCodeTool.vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import SplitScreenLayout from '~/components/report/SplitScreenLayout.vue'
import ReportHeader from '~/components/report/ReportHeader.vue'
import ReportAgentPanel from '~/components/report/ReportAgentPanel.vue'
import ChatSummary from '~/components/report/ChatSummary.vue'
import ForkBanner from '~/components/ForkBanner.vue'
import ForkedQueriesPanel from '~/components/ForkedQueriesPanel.vue'
import DashboardComponent from '~/components/DashboardComponent.vue'
import ArtifactFrame from '~/components/dashboard/ArtifactFrame.vue'
import CompletionItemFeedback from '~/components/CompletionItemFeedback.vue'
import TraceModal from '~/components/console/TraceModal.vue'
import QueryCodeEditorModal from '~/components/tools/QueryCodeEditorModal.vue'
import ImagePreviewModal from '~/components/ImagePreviewModal.vue'
import Spinner from '~/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { MarkdownRender } from 'markstream-vue'
import 'markstream-vue/index.css'

// Types
type ChatRole = 'user' | 'system'
type ChatStatus = 'in_progress' | 'success' | 'error' | 'stopped'

interface ToolCall {
	id: string
	tool_name: string
	tool_action?: string
	status: string
	result_summary?: string
	result_json?: any
	arguments_json?: any
	duration_ms?: number
	created_widget_id?: string
	created_step_id?: string
    created_widget?: any
    created_step?: any
    created_visualizations?: any[]
}

interface CompletionBlock {
	id: string
	seq?: number
	block_index: number
	phase?: string | null
	status: string
	content?: string
	reasoning?: string
	title?: string
	icon?: string
	started_at?: string
	completed_at?: string
	plan_decision?: {
		reasoning?: string
		assistant?: string
		final_answer?: string
		analysis_complete?: boolean
		plan_type?: string
	}
	tool_execution?: ToolCall
}

interface ChatMessage {
	id: string
	role: ChatRole
	status?: ChatStatus
	prompt?: { content: string }
	completion_blocks?: CompletionBlock[]
	tool_calls?: ToolCall[]
	created_at?: string
	// Backend system completion id used for sigkill
	system_completion_id?: string
	sigkill?: string | null
	feedback_score?: number
	// Transient streaming error message (set from SSE completion.error)
	error_message?: string
	// Optional structured error
	error?: any
	// Files attached to this completion (images, etc.)
	files?: { id: string; filename: string; content_type: string }[]
	// Instruction suggestions generated during this completion
	instruction_suggestions?: Array<{ text: string; category: string }>
	// Loading state for feedback-triggered suggestions
	instruction_suggestions_loading?: boolean
	// Scheduled prompt tag
	scheduled_prompt_id?: string | null
}

const { t, locale: i18nLocale } = useI18n({ useScope: 'global' })
const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
const isRtl = computed(() => RTL_LOCALES.has(i18nLocale.value))
const route = useRoute()
const report_id = (route.params.id as string) || ''

// Excel add-in mode detection (for compact UI)
const { isExcel, excelSelection } = useExcel()

// Permissions
const canViewConsole = computed(() => useCan('view_console'))

const messages = ref<ChatMessage[]>([])
const promptBoxRef = ref<InstanceType<typeof PromptBoxV2> | null>(null)

// List of queries for the summary pills — derived from created_steps in completions
const queryList = computed(() => {
	const list: { id: string; label: string; rowCount?: number; messageId: string; stepId: string }[] = []
	const seen = new Set<string>()
	for (const m of messages.value) {
		if (!m.completion_blocks) continue
		for (const b of m.completion_blocks) {
			const step = b.tool_execution?.created_step as any
			if (step && b.tool_execution?.status === 'success') {
				const stepId = step.id || step.query_id || ''
				if (stepId && seen.has(stepId)) continue
				if (stepId) seen.add(stepId)
				list.push({
					id: stepId,
					label: step.title || 'Query',
					rowCount: step.data?.info?.total_rows ?? undefined,
					messageId: m.id,
					stepId
				})
			}
		}
	}
	return list
})

const showContextIndicator = computed(() => {
	const completedSystem = messages.value.some(
		(m) => m.role === 'system' && ['success', 'error', 'stopped'].includes(m.status || '')
	)
	return completedSystem
})
// Pagination state
const pageLimit = 10
const hasMore = ref<boolean>(true)
const isLoadingMore = ref<boolean>(false)
const cursorBefore = ref<string | null>(null)
const promptText = ref<string>('')
const isStreaming = ref<boolean>(false)
// Tracks whether the main completion (analysis) is still running.
// Flips to false on completion.finished/error, even though isStreaming stays true
// for the knowledge harness tail. Used to unblock the prompt box early.
const isCompletionInProgress = ref<boolean>(false)
let currentController: AbortController | null = null
const scrollContainer = ref<HTMLElement | null>(null)
const scrollAnchor = ref<HTMLElement | null>(null)
// No absolute prompt box; no padding ref needed
// Scroll state tracking
const isUserAtBottom = ref<boolean>(true)
const suppressAutoScroll = ref<boolean>(false)
const lastScrollTop = ref<number>(0)
// Hysteresis thresholds
const NEAR_BOTTOM_PX = 96
const RETURN_TO_BOTTOM_PX = 12
// Debounced scroll scheduling during streaming
const pendingScroll = ref<boolean>(false)
let scrollRAF: number | null = null

// Trace modal state
const showTraceModal = ref(false)
const selectedCompletionForTrace = ref<string | null>(null)

// Report and Dashboard state
const reportLoaded = ref(false)
const reportNotFound = ref(false)
const completionsLoaded = ref(false)
const report = ref<any | null>(null)
const visualizations = ref<any[]>([])
const dashboardRef = ref<any | null>(null)
const textWidgetsIds = ref<string[]>([])

// Report summary (queries + instructions independent of message pagination)
const summaryQueries = ref<any[]>([])
const summaryInstructions = ref<any[]>([])
const pendingTrainingBuild = ref<{ id: string; status: string; total_instructions: number } | null>(null)
const pendingTrainingBuildDiff = ref<{ added_lines: number; removed_lines: number } | null>(null)

async function loadPendingBuildDiff() {
    const build = pendingTrainingBuild.value
    if (!build) { pendingTrainingBuildDiff.value = null; return }
    try {
        const mainRes = await useMyFetch<any>('/builds/main')
        const mainId = mainRes?.data?.value?.id
        if (!mainId || mainId === build.id) {
            pendingTrainingBuildDiff.value = { added_lines: 0, removed_lines: 0 }
            return
        }
        const { data } = await useMyFetch<any>(`/builds/${build.id}/diff/details?compare_to=${mainId}`)
        const items = (data?.value?.items || []) as any[]
        let added = 0, removed = 0
        for (const it of items) {
            const prev = (it.previous_text || '').split('\n')
            const next = (it.text || '').split('\n')
            const prevSet = new Set(prev)
            const nextSet = new Set(next)
            if (it.change_type === 'added') { for (const l of next) if (!prevSet.has(l)) added++ }
            else if (it.change_type === 'removed') { for (const l of prev) if (!nextSet.has(l)) removed++ }
            else {
                for (const l of next) if (!prevSet.has(l)) added++
                for (const l of prev) if (!nextSet.has(l)) removed++
            }
        }
        pendingTrainingBuildDiff.value = { added_lines: added, removed_lines: removed }
    } catch {
        pendingTrainingBuildDiff.value = null
    }
}

async function onApproveTrainingBuild(payload: { buildId: string; instructionIds: string[] } | string) {
    const buildId = typeof payload === 'string' ? payload : payload?.buildId
    const instructionIds = typeof payload === 'string' ? undefined : payload?.instructionIds
    if (!buildId) return
    try {
        const body: any = {}
        if (instructionIds && instructionIds.length > 0) body.instruction_ids = instructionIds
        const { error } = await useMyFetch(`/builds/${buildId}/publish`, { method: 'POST', body })
        if (error.value) throw error.value
        pendingTrainingBuild.value = null
        pendingTrainingBuildDiff.value = null
        await loadReportSummary()
        agentPanelRef.value?.refreshInstructions?.()
        mobileAgentPanelRef.value?.refreshInstructions?.()
    } catch (e) {
        console.error('Failed to approve training build', e)
    }
}

async function onDiscardTrainingBuild(buildId: string) {
    if (!buildId) return
    if (!confirm('Discard all staged instruction changes from this session?')) return
    try {
        const { error } = await useMyFetch(`/builds/${buildId}/reject`, {
            method: 'POST',
            body: { reason: 'discarded from training session pill' },
        })
        if (error.value) throw error.value
        pendingTrainingBuild.value = null
        await loadReportSummary()
    } catch (e) {
        console.error('Failed to discard training build', e)
    }
}

// Scheduled prompts state
const scheduledPrompts = ref<any[]>([])
const editingScheduledPrompt = ref<any>(null)
const showEditScheduledPromptModal = ref(false)
const expandedScheduledIds = ref<Set<string>>(new Set())

function toggleScheduledExpand(messageId: string) {
	if (expandedScheduledIds.value.has(messageId)) {
		expandedScheduledIds.value.delete(messageId)
	} else {
		expandedScheduledIds.value.add(messageId)
	}
}

function isScheduledExpanded(messageId: string): boolean {
	return expandedScheduledIds.value.has(messageId)
}

const showTrainingInstructionModal = ref(false)
const editingTrainingInstruction = ref<any>(null)

// Agent panel refs
const agentPanelRef = ref<InstanceType<typeof ReportAgentPanel> | null>(null)
const mobileAgentPanelRef = ref<InstanceType<typeof ReportAgentPanel> | null>(null)

// Live list of agents (data sources) selected in the prompt box — used to drive ReportAgentPanel
const currentAgents = ref<any[]>([])
watch(() => report.value?.data_sources, (val) => {
    if (val && currentAgents.value.length === 0) currentAgents.value = [...val]
}, { immediate: true })

async function openInstructionById(instructionId: string) {
	// Immediately switch to agent panel with loading state
	const panelRef = isMobile.value ? mobileAgentPanelRef : agentPanelRef
	if (isMobile.value) {
		mobileView.value = 'agent'
	} else {
		if (!isSplitScreen.value) isSplitScreen.value = true
		rightPanelView.value = 'agent'
	}
	await nextTick()
	panelRef.value?.setInstructionLoading(true)

	try {
		const { data, error } = await useMyFetch(`/instructions/${instructionId}`)
		if (!error.value && data.value) {
			panelRef.value?.openInstruction(data.value)
			return
		}
	} catch {}
	panelRef.value?.setInstructionLoading(false)
	// Fallback: open in modal if fetch failed
	editingTrainingInstruction.value = { id: instructionId }
	showTrainingInstructionModal.value = true
}

async function editTrainingInstruction(inst: { instructionId: string }) {
	try {
		const { data, error } = await useMyFetch(`/instructions/${inst.instructionId}`)
		if (!error.value && data.value) {
			editingTrainingInstruction.value = data.value
		} else {
			editingTrainingInstruction.value = { id: inst.instructionId }
		}
	} catch {
		editingTrainingInstruction.value = { id: inst.instructionId }
	}
	showTrainingInstructionModal.value = true
}

function visibleInstructions(m: ChatMessage) {
	return (m._loaded_instructions || []).filter((ins: any) => ins.category !== 'system')
}

function isScheduledSystemExpanded(msg: ChatMessage): boolean {
	// Find the preceding user message with the same scheduled_prompt_id
	const idx = messages.value.indexOf(msg)
	if (idx > 0) {
		const prev = messages.value[idx - 1]
		if (prev.scheduled_prompt_id === msg.scheduled_prompt_id && prev.role === 'user') {
			return expandedScheduledIds.value.has(prev.id)
		}
	}
	return true
}

function formatScheduledDate(date?: string) {
	if (!date) return ''
	return new Date(date).toLocaleString()
}

function getScheduledStats(userMsg: ChatMessage): string | null {
	// Find the paired system message
	const idx = messages.value.indexOf(userMsg)
	if (idx < 0 || idx >= messages.value.length - 1) return null
	const sysMsg = messages.value[idx + 1]
	if (!sysMsg || sysMsg.scheduled_prompt_id !== userMsg.scheduled_prompt_id || sysMsg.role !== 'system') return null
	const blocks = sysMsg.completion_blocks || []
	if (!blocks.length) return null

	let queries = 0
	let artifacts = 0
	for (const b of blocks) {
		const te = b.tool_execution
		if (!te || te.status !== 'success') continue
		if (te.tool_name === 'create_data' && te.created_step_id) queries++
		if (te.tool_name === 'create_artifact' || te.tool_name === 'edit_artifact') artifacts++
	}

	const parts: string[] = []
	parts.push(`${blocks.length} step${blocks.length !== 1 ? 's' : ''}`)
	if (queries) parts.push(`${queries} quer${queries !== 1 ? 'ies' : 'y'}`)
	if (artifacts) parts.push(`${artifacts} artifact${artifacts !== 1 ? 's' : ''}`)
	return parts.join(', ')
}

async function loadScheduledPrompts() {
    try {
        const { data } = await useMyFetch(`/reports/${report_id}/scheduled-prompts`)
        scheduledPrompts.value = (data.value as any[]) || []
    } catch {
        scheduledPrompts.value = []
    }
    // Start/stop the background poll based on whether this report has scheduled prompts
    if (scheduledPrompts.value.length > 0) {
        startScheduledCompletionsPoll()
    } else {
        stopScheduledCompletionsPoll()
    }
}

async function loadReportSummary() {
    try {
        const { data } = await useMyFetch(`/reports/${report_id}/summary`)
        const res = data.value as any
        summaryQueries.value = res?.queries || []
        summaryInstructions.value = (res?.instructions || []).map((i: any) => ({
            instructionId: i.instruction_id,
            title: i.title,
            category: i.category,
            isEdit: i.is_edit,
            lineCount: i.line_count,
            messageId: i.message_id,
            buildId: i.build_id,
        }))
        pendingTrainingBuild.value = res?.pending_training_build || null
        await loadPendingBuildDiff()
    } catch {
        summaryQueries.value = []
        summaryInstructions.value = []
        pendingTrainingBuild.value = null
        pendingTrainingBuildDiff.value = null
    }
}

async function deleteScheduledPrompt(sp: any) {
    try {
        await useMyFetch(`/reports/${report_id}/scheduled-prompts/${sp.id}`, { method: 'DELETE' })
        await loadScheduledPrompts()
    } catch {}
}

async function toggleScheduledPromptActive(sp: any) {
    try {
        await useMyFetch(`/reports/${report_id}/scheduled-prompts/${sp.id}`, {
            method: 'PUT',
            body: { is_active: !sp.is_active },
        })
        await loadScheduledPrompts()
    } catch {}
}

function editScheduledPrompt(sp: any) {
    editingScheduledPrompt.value = sp
    showEditScheduledPromptModal.value = true
}

// Fork state — extract forked queries and artifact ref from the fork summary completion
const forkedQueries = ref<any[]>([])

async function enrichForkedQueries() {
    const forkSummary = messages.value.find((m: any) => m.is_fork_summary)
    if (!forkSummary?.fork_asset_refs) {
        forkedQueries.value = []
        return
    }
    const queryRefs = (forkSummary.fork_asset_refs as any[]).filter((r: any) => r.type === 'query')
    const enriched = await Promise.all(queryRefs.map(async (qRef: any) => {
        try {
            const { data } = await useMyFetch(`/api/queries/${qRef.id}/default_step`)
            const step = (data.value as any)?.step || null
            const toolExecution = step ? {
                id: `fork-${qRef.id}`,
                tool_name: 'query',
                status: 'success',
                created_step: step,
            } : null
            return {
                id: qRef.id,
                title: qRef.title || 'Untitled Query',
                description: qRef.description || '',
                toolExecution,
            }
        } catch {
            return {
                id: qRef.id,
                title: qRef.title || 'Untitled Query',
                description: qRef.description || '',
                toolExecution: null,
            }
        }
    }))
    forkedQueries.value = enriched
}

const forkedArtifactRef = computed(() => {
    const forkSummary = messages.value.find((m: any) => m.is_fork_summary)
    if (!forkSummary?.fork_asset_refs) return null
    const artifactRef = (forkSummary.fork_asset_refs as any[]).find((ref: any) => ref.type === 'artifact')
    return artifactRef || null
})

const nonSeedMessages = computed(() => {
    return messages.value.filter((m: any) => !m.is_fork_summary)
})

// Split screen state
const isSplitScreen = ref(false)
const leftPanelWidth = ref(450)
const isResizing = ref(false)
const initialMouseX = ref(0)
const initialPanelWidth = ref(0)

// Right panel view mode
const rightPanelView = ref<'grid' | 'artifact' | 'agent' | 'summary'>('artifact')

// Mobile view mode (full-screen single section on narrow screens)
const mobileView = ref<'chat' | 'summary' | 'dashboard' | 'agent'>('chat')
const isMobile = ref(false)

function checkMobile() {
	isMobile.value = window.innerWidth < 768
}

if (import.meta.client) {
	checkMobile()
	window.addEventListener('resize', checkMobile)
}

// Completion id currently wired up to forward Office.js results back to the backend.
const currentOfficeJsCompletionId = ref<string | null>(null)

// Legacy report detection: has artifacts vs legacy dashboard_layout_versions
const hasArtifacts = ref(false)
const reportArtifacts = ref<any[]>([])
const hasLegacyLayout = ref(false)

// Toggle states
const collapsedReasoning = ref<Set<string>>(new Set())
const expandedToolDetails = ref<Set<string>>(new Set())
// Track blocks where user has manually toggled reasoning (so we don't auto-collapse them)
const manuallyToggledReasoning = ref<Set<string>>(new Set())



// Refs for reasoning content elements (used for dynamic ref binding)
const reasoningRefs = ref<Map<string, HTMLElement | null>>(new Map())

function setReasoningRef(blockId: string, el: HTMLElement | null) {
	if (el) {
		reasoningRefs.value.set(blockId, el)
	} else {
		reasoningRefs.value.delete(blockId)
	}
}

function scrollReasoningToBottom(blockId: string) {
	const el = reasoningRefs.value.get(blockId)
	if (el) {
		el.scrollTop = el.scrollHeight
	}
}


function isRealCompletion(m: ChatMessage): boolean {
    // During streaming we use a temporary client id like "system-<ts>".
    // Only allow feedback UI when we have a real backend id (UUID) either in
    // system_completion_id or in id.
    const cid = (m.system_completion_id || m.id) || ''
    // UUID v4 pattern (loose): 8-4-4-4-12 hex
    const uuidRe = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/
    return uuidRe.test(cid)
}

function getMessageError(m: any): string | null {
  // Prefer a content message stored on the completion (backend persisted), else last error block content
  try {
    // Some backends put message into completion.completion.content
    const content = (m?.completion?.content) || (m?.prompt?.content && m.status==='error' ? null : null)
    if (typeof content === 'string' && content.trim()) return content.trim()
  } catch {}
  const blocks = m?.completion_blocks || []
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]
    if (b?.status === 'error' && typeof b?.content === 'string' && b.content.trim()) {
      return b.content.trim()
    }
  }
  return null
}


// Helper functions for block types
function isBlockFinalized(block: CompletionBlock): boolean {
	return !!(block.plan_decision?.analysis_complete || block.completed_at || block.status === 'stopped')
}

function hasCompletedContent(block: CompletionBlock): boolean {
	return !!(block.content || block.tool_execution || block.status === 'completed' || block.status === 'stopped' || block.plan_decision?.analysis_complete || block.plan_decision?.final_answer)
}

function getToolComponent(toolName: string) {
	switch (toolName) {
    // 'create_data_model' removed
		case 'create_widget':
			return CreateWidgetTool
    case 'create_data':
      return CreateDataTool
			case 'describe_tables':
				return DescribeTablesTool
		case 'describe_entity':
			return DescribeEntityTool
		case 'create_and_execute_code':
			return ExecuteCodeTool
		case 'create_dashboard':
			return CreateDashboardTool
		case 'create_artifact':
			return CreateArtifactTool
		case 'read_artifact':
			return ReadArtifactTool
		case 'read_query':
			return ReadQueryTool
		case 'edit_artifact':
			return EditArtifactTool
		case 'answer_question':
			return AnswerQuestionTool
		case 'read_resources':
			return ReadResourcesTool
		case 'inspect_data':
			return InspectDataTool
		case 'search_mcps':
		case 'execute_mcp':
			return MCPTool
		case 'write_csv':
			return WriteCsvTool
		case 'write_to_excel':
			return WriteToExcelTool
		case 'write_officejs_code':
			return WriteOfficeJsCodeTool
		case 'read_excel_range':
			return ReadExcelRangeTool
		case 'read_excel_as_csv':
			return ReadExcelAsCsvTool
		case 'suggest_instructions':
			return InstructionSuggestions
		case 'create_instruction':
			return CreateInstructionTool
		case 'edit_instruction':
			return EditInstructionTool
		case 'search_instructions':
			return SearchInstructionsTool
		case 'execute_code':
		case 'execute_sql':
			return ExecuteCodeTool
		default:
			return null
	}
}

function shouldUseToolComponent(toolExecution: ToolCall): boolean {
	return getToolComponent(toolExecution.tool_name) !== null
}

function shouldShowToolWidgetPreview(toolExecution: ToolCall | undefined): boolean {
	if (!toolExecution) return false
	
  // Only show for generic code-execution tools with success status.
  // Tools with a specialized component (e.g., create_widget, create_data) handle their own preview.
  const showForTools = ['create_and_execute_code', 'execute_code', 'execute_sql']
	return showForTools.includes(toolExecution.tool_name) && 
	       toolExecution.status === 'success' &&
	       (toolExecution.created_widget || toolExecution.created_step)
}

function shouldShowWorkingDots(message: ChatMessage): boolean {
	// Only show for system messages that are in progress
	if (message.role !== 'system' || message.status !== 'in_progress') {
		return false
	}
	
	// Don't show dots if the message was killed (sigkill timestamp exists)
	if (message.sigkill) {
		return false
	}
	
	// CASE 1: No blocks yet - show dots (initial startup phase)
	if (!message.completion_blocks || message.completion_blocks.length === 0) {
		return true
	}
	
	// CASE 2: Blocks exist but no meaningful content yet (early startup)
	const hasAnyMeaningfulContent = message.completion_blocks.some(block => 
		block.plan_decision?.reasoning || 
		block.reasoning || 
		block.content ||
		block.tool_execution
	)
	
	// If no meaningful content yet, show dots
	if (!hasAnyMeaningfulContent) {
		return true
	}
	
	// CASE 3: Check if we're in a "gap" between blocks during streaming
	const lastBlock = message.completion_blocks[message.completion_blocks.length - 1]
	
	// If the last block has final_answer and analysis_complete, we're truly done
	if (lastBlock?.plan_decision?.analysis_complete === true) {
		return false
	}
	
	// Check if the last block has finished its main content but no tools are running
	const lastBlockHasContent = lastBlock && (
		lastBlock.content ||
		lastBlock.plan_decision?.final_answer
	)
	
	// Check if tools are actively running
	const hasActiveTools = message.completion_blocks.some(block => 
		block.tool_execution?.status === 'running' || 
		block.status === 'in_progress'
	)
	
	// Check if any block is actively streaming text (has reasoning but no assistant yet)
	const hasStreamingContent = message.completion_blocks.some(block => 
		(block.plan_decision?.reasoning && !block.content) ||
		(block.reasoning && !block.content)
	)
	
	// Show dots when:
	// 1. System is in progress AND
	// 2. No active tools/streaming AND
	// 3. Last block has content but system continues (preparing next block)
	return !hasActiveTools && !hasStreamingContent && (!!lastBlockHasContent && message.status === 'in_progress')
}

function getThoughtProcessLabel(block: CompletionBlock): string {
	// Handle stopped blocks
	if (block.status === 'stopped') {
		return t('reportView.thoughtProcess')
	}

	// Prefer planner-provided reasoning duration when available
	const metricsAny: any = (block.plan_decision as any)?.metrics || (block.plan_decision as any)?.metrics_json
	const thinkingMs: number | undefined = metricsAny?.thinking_ms
	if (typeof thinkingMs === 'number' && isFinite(thinkingMs) && thinkingMs >= 0) {
		const secs = Math.max(0, Math.round(thinkingMs / 1000))
		return t('reportView.thoughtForSeconds', { seconds: secs })
	}

	// Calculate duration from started_at to completed_at if available
	if (block.started_at && block.completed_at) {
		const startTime = new Date(block.started_at).getTime()
		const endTime = new Date(block.completed_at).getTime()
		const durationMs = endTime - startTime
		const durationSeconds = Math.round(durationMs / 1000)

		// Sanity check for unreasonable durations (over 30 minutes)
		if (durationSeconds > 1800) {
			return t('reportView.stopped')
		}

		return t('reportView.thoughtForSeconds', { seconds: durationSeconds })
	}

	// Fallback to duration from tool execution if available
	if (block.tool_execution?.duration_ms) {
		const durationSeconds = (block.tool_execution.duration_ms / 1000).toFixed(1)
		return t('reportView.thoughtForSeconds', { seconds: durationSeconds })
	}

	// Default fallback
	return t('reportView.thoughtProcess')
}



// Auto-collapse reasoning when content becomes available (but respect user's manual toggle)
// Only watch the last system message to avoid iterating ALL messages on every token
const lastSystemMessage = computed(() => 
	[...messages.value].reverse().find(m => m.role === 'system')
)

watch(
	// Watch only block IDs and their completion status, not deep content
	() => lastSystemMessage.value?.completion_blocks?.map(b => ({
		id: b.id,
		hasContent: hasCompletedContent(b),
		hasTool: !!b.tool_execution
	})),
	(blocks) => {
		if (!blocks) return
		for (const b of blocks) {
			// Auto-collapse when content exists OR when tool execution exists
			if ((b.hasContent || b.hasTool) && !collapsedReasoning.value.has(b.id) && !manuallyToggledReasoning.value.has(b.id)) {
				collapsedReasoning.value.add(b.id)
			}
		}
	},
	{ deep: true }
)

// Watch for split screen changes and scroll to bottom to maintain position
watch(() => isSplitScreen.value, () => {
    nextTick(() => setTimeout(safeScrollToBottom, 80))
})

// Adjust left panel width based on active right panel tab
watch(rightPanelView, (view) => {
    if (!isSplitScreen.value || isResizing.value) return
    const windowWidth = window.innerWidth
    if (view === 'summary') {
        leftPanelWidth.value = Math.round(windowWidth * 0.55)
    } else if (view === 'agent') {
        leftPanelWidth.value = Math.round(windowWidth * 0.45)
        collapseSidebar()
    } else {
        leftPanelWidth.value = Math.round(windowWidth * 0.37)
        collapseSidebar()
    }
})

function goBack() {
	if (history.length > 1) history.back()
}

function toggleReasoning(messageId: string) {
	// Mark as manually toggled so auto-collapse won't override user's choice
	manuallyToggledReasoning.value.add(messageId)
	if (collapsedReasoning.value.has(messageId)) {
		collapsedReasoning.value.delete(messageId)
	} else {
		collapsedReasoning.value.add(messageId)
	}
}

function isReasoningCollapsed(messageId: string) {
	return collapsedReasoning.value.has(messageId)
}

function toggleToolDetails(toolId: string) {
	if (expandedToolDetails.value.has(toolId)) {
		expandedToolDetails.value.delete(toolId)
	} else {
		expandedToolDetails.value.add(toolId)
	}
}

function isToolDetailsExpanded(toolId: string) {
	return expandedToolDetails.value.has(toolId)
}

// Get attached images from a message's files
function getAttachedImages(message: ChatMessage) {
	const files = message.files || []
	return files.filter((f: any) => (f.content_type || '').startsWith('image/'))
}

// Image preview modal
const imagePreviewModalRef = ref<InstanceType<typeof ImagePreviewModal> | null>(null)

function openImagePreview(file: any) {
	imagePreviewModalRef.value?.open(file)
}

function scrollToMessage(messageId: string, stepId?: string) {
	const container = scrollContainer.value
	if (!container) return
	// If a stepId is provided, try to scroll to the specific tool execution block first
	if (stepId) {
		const stepEl = container.querySelector(`[data-step-id="${stepId}"]`) as HTMLElement
		if (stepEl) {
			stepEl.scrollIntoView({ behavior: 'smooth', block: 'center' })
			return
		}
	}
	const el = container.querySelector(`[data-message-id="${messageId}"]`) as HTMLElement
	if (el) {
		el.scrollIntoView({ behavior: 'smooth', block: 'center' })
	}
}

function scrollToBottom() {
  // Single-pass scroll: go to max scroll position
  nextTick(() => {
    setTimeout(() => {
      const container = scrollContainer.value
      if (!container) return
      container.offsetHeight // force reflow
      container.scrollTop = container.scrollHeight
    }, 40)
  })
}

// Guarded scroll that respects user upward scrolling during streaming
function safeScrollToBottom() {
  if (isStreaming.value && suppressAutoScroll.value) return
  scrollToBottom()
}

// Only auto-scroll when the user is already near the bottom to avoid jumpiness
function autoScrollIfNearBottom() {
  const container = scrollContainer.value
  if (!container) return
  const threshold = NEAR_BOTTOM_PX
  const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight)
  if (suppressAutoScroll.value && isStreaming.value) return
  if (distanceFromBottom <= threshold) {
    scrollToBottom()
  }
}

function scheduleInitialScroll() {
    const delays = [0, 80, 160, 320, 640]
    for (const delay of delays) setTimeout(safeScrollToBottom, delay)
}

// Keep scrolling to bottom across successive layout passes until height stabilizes
function settleScrollToBottom(maxFrames = 24) {
    const container = scrollContainer.value
    if (!container) return
    let frames = 0
    let lastHeight = -1
    const tick = () => {
        if (!scrollContainer.value) return
        const h = scrollContainer.value.scrollHeight
        if (h !== lastHeight) {
            lastHeight = h
            scrollContainer.value.scrollTop = h
            frames = 0
        } else {
            frames++
        }
        if (frames < 3 && maxFrames-- > 0) {
            requestAnimationFrame(tick)
        }
    }
    requestAnimationFrame(tick)
}

async function handleStreamingEvent(eventType: string | null, payload: any, sysMessageIndex: number) {
	if (!eventType || sysMessageIndex === -1) return
	
	if (!messages.value[sysMessageIndex]) return

	const sysMessage = messages.value[sysMessageIndex]
	
	switch (eventType) {
		case 'completion.started':
			// Update system message status
			sysMessage.status = 'in_progress'
			// Stash backend system completion id for stop-generation (sigkill)
			if (payload && payload.system_completion_id) {
				sysMessage.system_completion_id = payload.system_completion_id
				currentOfficeJsCompletionId.value = payload.system_completion_id
			}
			break

		case 'instructions.context':
			// Track which instructions were loaded (context build or tool calls)
			if (!sysMessage._loaded_instructions) sysMessage._loaded_instructions = []
			for (const inst of (payload?.instructions || [])) {
				if (inst?.id && !sysMessage._loaded_instructions.some((i: any) => i.id === inst.id)) {
					sysMessage._loaded_instructions.push({ ...inst, source: payload.source || 'context_build' })
				}
			}
			break

		case 'instructions.suggest.started':
			// Flip a flag so <KnowledgeGroup> renders immediately in a loading
			// state, even before the first harness block arrives.
			;(sysMessage as any)._harness_running = true
			break

		case 'instructions.suggest.partial':
			break
		case 'instructions.suggest.finished':
			;(sysMessage as any)._harness_running = false
			break

		case 'block.upsert':
			// Add or update a completion block
			if (payload.block) {
				const block = payload.block
				if (!sysMessage.completion_blocks) {
					sysMessage.completion_blocks = []
				}

				// Find existing block or insert in-order by block_index (avoid resorting array)
				const existingIndex = sysMessage.completion_blocks.findIndex(b => b.id === block.id)
				if (existingIndex >= 0) {
					// Update existing block in place
					Object.assign(sysMessage.completion_blocks[existingIndex], block)
				} else {
					let insertPos = sysMessage.completion_blocks.length
					for (let i = 0; i < sysMessage.completion_blocks.length; i++) {
						const bi = sysMessage.completion_blocks[i]
						if ((bi?.block_index ?? Number.MAX_SAFE_INTEGER) > (block?.block_index ?? Number.MAX_SAFE_INTEGER)) {
							insertPos = i
							break
						}
					}
					sysMessage.completion_blocks.splice(insertPos, 0, block)
				}
			}
			break

		case 'block.delta.text':
			// Update text snapshot for a specific block (full overwrite)
			// Mutate in-place to avoid triggering full array reactivity
			if (payload.block_id && payload.field && payload.text) {
				const block = sysMessage.completion_blocks?.find(b => b.id === payload.block_id)
				if (block) {
					if (payload.field === 'content') {
						block.content = payload.text
					} else if (payload.field === 'reasoning') {
						block.reasoning = payload.text
						if (!block.plan_decision) block.plan_decision = {}
						block.plan_decision.reasoning = payload.text
						// Auto-scroll reasoning box
						nextTick(() => scrollReasoningToBottom(payload.block_id))
					}
				}
			}
			break

		case 'block.delta.token':
			// Handle individual token streaming for real-time typing effect
			// Mutate in-place to avoid triggering full array reactivity on every token
			if (payload.block_id && payload.field && payload.token) {
				const block = sysMessage.completion_blocks?.find(b => b.id === payload.block_id)
				if (block) {
					const t = String(payload.token || '')
					if (payload.field === 'content') {
						block.content = (block.content || '') + t
					} else if (payload.field === 'reasoning') {
						block.reasoning = (block.reasoning || '') + t
						if (!block.plan_decision) block.plan_decision = {}
						block.plan_decision.reasoning = (block.plan_decision.reasoning || '') + t
						// Auto-scroll reasoning box
						nextTick(() => scrollReasoningToBottom(payload.block_id))
					}
				}
			}
			break

		case 'block.delta.text.complete':
			// Field finalization marker — no action needed, MarkdownRender handles it via :final
			break

		case 'block.delta.artifact':
			// Handle artifact changes (for progressive updates)
			if (payload.change && payload.change.type === 'step') {
				const block = sysMessage.completion_blocks?.find(b => b.tool_execution?.created_step_id === payload.change.step_id)
				if (block && block.tool_execution) {
					block.status = 'in_progress'
					// Merge streamed data_model fields into tool_execution.result_json for live UI updates
					const fields = payload.change.fields || {}
					if (fields.data_model) {
						block.tool_execution.result_json = block.tool_execution.result_json || {}
						const rj: any = block.tool_execution.result_json
						rj.data_model = { ...(rj.data_model || {}), ...fields.data_model }
						if (Array.isArray(fields.data_model.columns)) {
							const existing = new Map<string, any>((rj.data_model.columns || []).map((c: any) => [c.generated_column_name, c]))
							for (const col of fields.data_model.columns) existing.set(col.generated_column_name, col)
							rj.data_model.columns = Array.from(existing.values())
						}
					}
				}
			}
			break

		case 'tool.started':
			// Update block to show tool execution started
			if (payload.tool_name) {
				// Find the most recent block and update it
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.tool_execution) {
						lastBlock.tool_execution = {
							id: `temp-${Date.now()}`,
							tool_name: payload.tool_name,
							status: 'running'
						}
					}
					// Reset result_json for fresh run to avoid stale shared references
					lastBlock.tool_execution.result_json = {}
					// For describe_tables, stash the query so the UI can show it
					try {
						if (payload.tool_name === 'describe_tables' && payload.arguments) {
							const q = payload.arguments.query
							const qStr = Array.isArray(q) ? q.join(', ') : (typeof q === 'string' ? q : (q ? JSON.stringify(q) : 'tables'))
							;(lastBlock.tool_execution.result_json as any).search_query = q
							lastBlock.tool_execution.result_summary = `Searching ${qStr}…`
						}
						if (payload.tool_name === 'read_resources' && payload.arguments) {
							const q = payload.arguments.query
							const qStr = Array.isArray(q) ? q.join(', ') : (typeof q === 'string' ? q : (q ? JSON.stringify(q) : 'resources'))
							;(lastBlock.tool_execution.result_json as any).search_query = q
							lastBlock.tool_execution.result_summary = `Searching ${qStr}…`
						}
						if (payload.tool_name === 'describe_entity' && payload.arguments) {
							const nameOrId = payload.arguments.name_or_id || 'entity'
							;(lastBlock.tool_execution as any).arguments_json = payload.arguments
							lastBlock.tool_execution.result_summary = `Loading from catalog: "${nameOrId}"…`
						}
						if (payload.tool_name === 'create_artifact' && payload.arguments) {
							;(lastBlock.tool_execution as any).arguments_json = payload.arguments
							;(lastBlock.tool_execution as any).report_id = report_id
							const modeLabel = payload.arguments.mode === 'slides' ? 'presentation' : 'dashboard'
							lastBlock.tool_execution.result_summary = `Creating ${modeLabel}: "${payload.arguments.title || 'Untitled'}"…`
						}
						if (payload.tool_name === 'edit_artifact' && payload.arguments) {
							;(lastBlock.tool_execution as any).arguments_json = payload.arguments
						}
						if (payload.tool_name === 'inspect_data' && payload.arguments) {
							;(lastBlock.tool_execution as any).arguments_json = payload.arguments
						}
						if ((payload.tool_name === 'execute_mcp' || payload.tool_name === 'search_mcps') && payload.arguments) {
							;(lastBlock.tool_execution as any).arguments_json = payload.arguments
						}
						if ((payload.tool_name === 'create_instruction' || payload.tool_name === 'edit_instruction') && payload.arguments) {
							;(lastBlock.tool_execution as any).arguments_json = payload.arguments
						}
						if (payload.tool_name === 'search_instructions' && payload.arguments) {
							;(lastBlock.tool_execution as any).arguments_json = payload.arguments
							const q = payload.arguments.query
							const qStr = Array.isArray(q) ? q.join(', ') : (typeof q === 'string' ? q : (q ? JSON.stringify(q) : 'instructions'))
							;(lastBlock.tool_execution.result_json as any).search_query = q
							lastBlock.tool_execution.result_summary = `Searching instructions for ${qStr}…`
						}
					} catch {}
					lastBlock.status = 'in_progress'
				}
			}
			break

		case 'tool.progress':
			// Update tool execution progress on the latest block (best-effort) and stream data model deltas
			if (payload.tool_name) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.tool_execution) {
						lastBlock.tool_execution = {
							id: `temp-${Date.now()}`,
							tool_name: payload.tool_name,
							status: 'running'
						}
					} else {
						lastBlock.tool_execution.status = 'running'
					}

					// Best-effort cancel of a running Office.js execution in the taskpane
					// (sigkill or timeout path from the backend tool).
					const cancelAction = payload.payload?.excel_action
					if (cancelAction && cancelAction.type === 'cancelOfficeJs' && isExcel.value) {
						try {
							window.parent.postMessage({
								type: 'cancelOfficeJs',
								data: JSON.stringify(cancelAction)
							}, window.location.origin)
						} catch (e) {
							console.warn('Failed to forward cancelOfficeJs to Excel taskpane:', e)
						}
					}

					// Record progress stage for tool-specific UIs
					if (payload.payload && lastBlock.tool_execution) {
						;(lastBlock.tool_execution as any).progress_stage = payload.payload.stage || null
						// Capture icon for read_resources submit_search stage if provided
						if (payload.tool_name === 'read_resources' && payload.payload.stage === 'submit_search' && payload.payload.icon) {
							lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
							;(lastBlock.tool_execution.result_json as any).icon = payload.payload.icon
						}
						// Capture connection_name for execute_mcp when resolved
						if (payload.tool_name === 'execute_mcp' && payload.payload.stage === 'connection_resolved' && payload.payload.connection_name) {
							lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
							;(lastBlock.tool_execution.result_json as any).connection_name = payload.payload.connection_name
						}

						// Capture code, attempt, and errors for create_data / inspect_data
						if ((payload.tool_name === 'create_data' || payload.tool_name === 'inspect_data') && payload.payload) {
							const p = payload.payload
							const te = lastBlock.tool_execution as any
							// Stream generated code from code_generated stage
							if (p.stage === 'generated_code' && p.code) {
								te.progress_code = p.code
							}
							// Track current attempt number
							if (typeof p.attempt === 'number') {
								te.progress_attempt = p.attempt
							}
							// On retry, capture the error that triggered it
							if (p.stage === 'retry') {
								te.progress_errors = te.progress_errors || []
								// The error was already emitted via stdout before the retry event
							}
						}
					}

          // Progressive data model updates for create_widget tool
          if ((payload.tool_name === 'create_widget') && payload.payload) {
						const p = payload.payload
						// Ensure result_json.data_model structure exists
						lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
						const rj = lastBlock.tool_execution.result_json as any
						rj.data_model = rj.data_model || { type: null, columns: [], series: [] }

						if (p.stage === 'data_model_type_determined' && p.data_model_type) {
							rj.data_model.type = p.data_model_type
						}
						if (p.stage === 'column_added' && p.column) {
							const exists = (rj.data_model.columns || []).some((c: any) => c.generated_column_name === p.column.generated_column_name)
							if (!exists) {
								rj.data_model.columns.push(p.column)
							}
						}
						if (p.stage === 'series_configured' && Array.isArray(p.series)) {
							rj.data_model.series = p.series
						}
						if (p.stage === 'widget_creation_needed' && p.data_model) {
							rj.data_model = { ...rj.data_model, ...p.data_model }
						}
					}

					// Progressive visualization updates for create_data tool
					if (payload.tool_name === 'create_data' && payload.payload?.stage === 'visualization_inferred') {
						const p = payload.payload
						;(lastBlock.tool_execution as any).progress_visualization = {
							chart_type: p.chart_type,
							series: p.series || [],
							group_by: p.group_by
						}
					}
					// Visualization error for create_data tool
					if (payload.tool_name === 'create_data' && payload.payload?.stage === 'visualization_error') {
						;(lastBlock.tool_execution as any).progress_visualization_error = payload.payload.error
					}

					// Progressive instruction drafts for suggest_instructions tool
					if (payload.tool_name === 'suggest_instructions' && payload.payload) {
						const p = payload.payload
						if (p.stage === 'instruction_added' && p.instruction) {
							lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
							const rj: any = lastBlock.tool_execution.result_json
							rj.drafts = Array.isArray(rj.drafts) ? rj.drafts : []
							const draft = { text: String(p.instruction.text || ''), category: p.instruction.category || null }
							if (draft.text) {
								rj.drafts.push(draft)
								lastBlock.status = 'in_progress'
							}
						}
					}

					// When create_dashboard streams a completed block, broadcast layout change so previews refresh membership
					if (payload.tool_name === 'create_dashboard' && payload.payload && payload.payload.stage === 'block.completed') {
						try {
							window.dispatchEvent(new CustomEvent('dashboard:layout_changed', { detail: { report_id: report_id, action: 'added' } }))
						} catch {}
					}

					// Visualizations resolved for create_artifact / edit_artifact
					if ((payload.tool_name === 'create_artifact' || payload.tool_name === 'edit_artifact') && payload.payload) {
						if (payload.payload.stage === 'visualizations_resolved' && Array.isArray(payload.payload.visualizations)) {
							;(lastBlock.tool_execution as any).progress_visualizations = payload.payload.visualizations
						}
					}

					// Progressive slide tracking for create_artifact tool
					if (payload.tool_name === 'create_artifact' && payload.payload) {
						const p = payload.payload
						// Artifact created with pending status - notify frontend
						if (p.stage === 'artifact_created' && p.artifact_id) {
							;(lastBlock.tool_execution as any).pending_artifact_id = p.artifact_id
							// Dispatch event so ArtifactFrame can show the pending artifact
							hasArtifacts.value = true
							try {
								window.dispatchEvent(new CustomEvent('artifact:created', {
									detail: {
										report_id: report_id,
										artifact_id: p.artifact_id,
										status: 'pending'
									}
								}))
							} catch {}
						}
						// Track generating progress
						if (p.stage === 'generating') {
							;(lastBlock.tool_execution as any).progress_stage = 'generating'
							;(lastBlock.tool_execution as any).progress_payload = { chars: p.chars }
						}
						// Track slides as they're generated
						if (p.stage === 'slide_generated') {
							;(lastBlock.tool_execution as any).progress_stage = 'generating_slides'
							const slides = (lastBlock.tool_execution as any).progress_slides || []
							// Mark previous slides as done
							for (let i = 0; i < slides.length; i++) {
								slides[i].status = 'done'
							}
							// Add new slide as generating
							while (slides.length <= p.slide_index) {
								slides.push({ status: slides.length === p.slide_index ? 'generating' : 'done' })
							}
							;(lastBlock.tool_execution as any).progress_slides = slides
						}
						// Store artifact info from arguments
						if (p.title) {
							lastBlock.tool_execution.arguments_json = lastBlock.tool_execution.arguments_json || {}
							;(lastBlock.tool_execution.arguments_json as any).title = p.title
						}
					}

					lastBlock.status = 'in_progress'
				}
			}
			break

		case 'tool.stdout':
			// Capture stdout messages (errors, execution logs) for create_data / inspect_data
			if (payload.tool_name) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock?.tool_execution) {
					const te = lastBlock.tool_execution as any
					te.progress_stdout = te.progress_stdout || []
					const msg = typeof payload.payload === 'string' ? payload.payload : (payload.payload?.message || '')
					if (msg) {
						te.progress_stdout.push(msg)
					}
				}
			}
			break

		case 'tool.confirmation':
			// Confirmation request from create_artifact / edit_artifact
			if (payload.tool_name) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock?.tool_execution) {
					;(lastBlock.tool_execution as any).confirmation = payload.payload
					;(lastBlock.tool_execution as any).progress_stage = 'awaiting_confirmation'
				}
			}
			break

		case 'tool.partial':
			// Streamed partial output for tools (e.g., answer_question)
			if (payload.tool_name) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.tool_execution) {
						lastBlock.tool_execution = {
							id: `temp-${Date.now()}`,
							tool_name: payload.tool_name,
							status: 'running'
						}
					}
					const fullAnswer = (payload.payload && typeof payload.payload.answer === 'string') ? payload.payload.answer : null
					const delta = (payload.payload && typeof payload.payload.delta === 'string') ? payload.payload.delta : null
					lastBlock.tool_execution.result_json = lastBlock.tool_execution.result_json || {}
					const rj: any = lastBlock.tool_execution.result_json
					if (fullAnswer !== null) {
						// Replace with accumulated answer (preferred)
						rj.answer = fullAnswer
						lastBlock.status = 'in_progress'
					} else if (delta) {
						// Backward-compatibility: append streaming delta
						rj.answer = (rj.answer || '') + delta
						lastBlock.status = 'in_progress'
					}
					// Forward Office.js code execution to the Excel taskpane.
					const excelAction = payload.payload?.excel_action
					if (excelAction && excelAction.type === 'runOfficeJs' && isExcel.value) {
						try {
							window.parent.postMessage({
								type: 'runOfficeJs',
								data: JSON.stringify(excelAction)
							}, window.location.origin)
						} catch (e) {
							console.warn('Failed to forward runOfficeJs to Excel taskpane:', e)
						}
						if (lastBlock.tool_execution) {
							lastBlock.tool_execution.arguments_json = lastBlock.tool_execution.arguments_json || {}
							const aj: any = lastBlock.tool_execution.arguments_json
							if (excelAction.code && !aj.code) aj.code = excelAction.code
							if (excelAction.description && !aj.description) aj.description = excelAction.description
						}
					}
				}
			}
			break

		case 'widget.created':
			// No-op for now; this is displayed in the report UI elsewhere
			break

		case 'data_model.completed':
			// No-op; step/widget UIs will reflect final data model. Avoid logging unknown.
			break

		case 'tool.finished':
			// Update tool execution status
			if (payload.tool_name && payload.status) {
				// Prefer precise targeting when identifiers are available
				const blocks = sysMessage.completion_blocks || []
				let blockWithTool = blocks.find(b => (payload.block_id && b.id === payload.block_id)) 
					|| blocks.find(b => (payload.tool_execution_id && b.tool_execution?.id === payload.tool_execution_id))
					// Fallback: choose the most recent running/in-progress block for this tool
					|| [...blocks].reverse().find(b => 
						b.tool_execution?.tool_name === payload.tool_name && 
						(b.tool_execution?.status === 'running' || b.status === 'in_progress')
					)
					// Last fallback: most recent block with matching tool name
					|| [...blocks].reverse().find(b => b.tool_execution?.tool_name === payload.tool_name)

				if (blockWithTool?.tool_execution) {
					blockWithTool.tool_execution.status = payload.status
					blockWithTool.status = payload.status === 'success' ? 'success' : payload.status === 'stopped' ? 'stopped' : 'error'
					if (payload.result_summary) {
						blockWithTool.tool_execution.result_summary = payload.result_summary
					}
					if (payload.result_json) {
						blockWithTool.tool_execution.result_json = payload.result_json
					}
					if (payload.duration_ms !== undefined) {
						blockWithTool.tool_execution.duration_ms = payload.duration_ms
					}
					if (payload.created_widget_id) {
						blockWithTool.tool_execution.created_widget_id = payload.created_widget_id
					}
					if (payload.created_step_id) {
						blockWithTool.tool_execution.created_step_id = payload.created_step_id
					}
					// Populate created_visualizations from the IDs sent by backend
					if (payload.created_visualization_ids && Array.isArray(payload.created_visualization_ids) && payload.created_visualization_ids.length > 0) {
						blockWithTool.tool_execution.created_visualizations = payload.created_visualization_ids.map((id: string) => ({ id }))
					}
					// If the dashboard was created successfully, refresh widgets and open the dashboard pane
					if (payload.tool_name === 'create_dashboard' && payload.status === 'success') {
						try { await loadVisualizations() } catch (e) { /* noop */ }
						if (!isSplitScreen.value) toggleSplitScreen()
					}
					// If the artifact was created successfully, mark all slides as done and dispatch event
					if (payload.tool_name === 'create_artifact' && payload.status === 'success') {
						// Mark all slides as done
						const slides = (blockWithTool.tool_execution as any).progress_slides || []
						for (const slide of slides) {
							slide.status = 'done'
						}
						// Update hasArtifacts state and dispatch event to notify ArtifactFrame
						hasArtifacts.value = true
						try {
							window.dispatchEvent(new CustomEvent('artifact:created', {
								detail: {
									report_id: report_id,
									artifact_id: payload.result_json?.artifact_id
								}
							}))
						} catch {}
					}
					// If artifact was edited successfully, refresh ArtifactFrame with the new version
					if (payload.tool_name === 'edit_artifact' && payload.status === 'success') {
						hasArtifacts.value = true
						try {
							window.dispatchEvent(new CustomEvent('artifact:created', {
								detail: {
									report_id: report_id,
									artifact_id: payload.result_json?.artifact_id
								}
							}))
						} catch {}
					}
					// If write_to_excel completed, forward data to Excel taskpane via postMessage
					if (payload.tool_name === 'write_to_excel' && payload.status === 'success' && payload.result_json?.excel_action && isExcel.value) {
						try {
							const action = payload.result_json.excel_action
							window.parent.postMessage({
								type: action.type,
								data: JSON.stringify(action.data)
							}, window.location.origin)
						} catch (e) {
							console.warn('Failed to forward write_to_excel data to Excel taskpane:', e)
						}
					}
				}
			}
			break

		case 'decision.partial':
		case 'decision.final':
			// Update plan decision information
			// Note: decision.final events may only contain analysis_complete/final_answer without reasoning/assistant
			if (payload.reasoning || payload.assistant || payload.final_answer !== undefined || payload.analysis_complete !== undefined) {
				const lastBlock = sysMessage.completion_blocks?.[sysMessage.completion_blocks.length - 1]
				if (lastBlock) {
					if (!lastBlock.plan_decision) {
						lastBlock.plan_decision = {}
					}
					if (payload.reasoning) {
						lastBlock.plan_decision.reasoning = payload.reasoning
					}
					if (payload.assistant) {
						lastBlock.plan_decision.assistant = payload.assistant
					}
					if (payload.final_answer) {
						lastBlock.plan_decision.final_answer = payload.final_answer
					}
					if (eventType === 'decision.final') {
						lastBlock.plan_decision.analysis_complete = payload.analysis_complete ?? true
					}
				}
			}
			break

		case 'completion.finished':
			// Mark completion as finished with proper status if provided; don't default to success
			const completionStatus = (payload && typeof payload.status === 'string') ? payload.status : null
			if (completionStatus) {
				sysMessage.status = completionStatus as any
				if (completionStatus === 'error' && payload?.error?.message) {
					sysMessage.error_message = String(payload.error.message)
					// Ensure a single error block exists for history (won't render duplicate due to block suppression)
					if (!sysMessage.completion_blocks?.some((b: any) => b.status === 'error')) {
						sysMessage.completion_blocks = sysMessage.completion_blocks || []
						sysMessage.completion_blocks.push({ id: `error-${Date.now()}`, block_index: 999, status: 'error', content: sysMessage.error_message })
					}
				}
				// NOTE: do NOT flip isStreaming here. The knowledge harness continues
				// streaming SSE events (block.upsert/tool.*) after completion.finished
				// fires. Flipping isStreaming=false early opens a race window where
				// polling/refetch paths can wipe messages.value mid-stream. [DONE] is
				// the single source of truth for end-of-stream. Thumbs-up and
				// stop→submit UI should gate on sysMessage.status, not isStreaming.
			}
			// Unblock the prompt box so the user can submit new prompts while
			// the knowledge harness continues in the background.
			isCompletionInProgress.value = false
			// Note: loadReport and refreshContextEstimate are called after [DONE] to avoid blocking
			break

		case 'completion.error':
			// Dedicated error event; ensure UI flips to error state and capture the message
			sysMessage.status = 'error'
			isCompletionInProgress.value = false
			if (payload?.error) {
				const msg = typeof payload.error === 'string' ? payload.error : (payload.error.message || '')
				if (msg) sysMessage.error_message = String(msg)
				if (!sysMessage.completion_blocks?.some((b: any) => b.status === 'error')) {
					sysMessage.completion_blocks = sysMessage.completion_blocks || []
					sysMessage.completion_blocks.push({ id: `error-${Date.now()}`, block_index: 999, status: 'error', content: sysMessage.error_message })
				}
			}
			break

		default:
			// Handle unknown events gracefully
			break
	}
}

async function loadCompletions({ skipEstimate = false } = {}) {
	try {
		const { data } = await useMyFetch(`/reports/${report_id}/completions?limit=${pageLimit}`)
		const response = data.value as any
		const list = response?.completions || []
		messages.value = list.map((c: any) => {
			// Override status if sigkill timestamp exists - this means it was stopped
			let status = c.status as ChatStatus
			if (c.sigkill && status === 'in_progress') {
				status = 'stopped'
			}
			
			const blocks = c.completion_blocks?.map((b: any) => ({
				id: b.id,
				seq: b.seq,
				block_index: b.block_index,
				loop_index: b.loop_index,
				phase: b.phase,
				title: b.title,
				icon: b.icon,
				status: b.status,
				content: b.content,
				reasoning: b.reasoning,
				plan_decision: b.plan_decision,
				tool_execution: b.tool_execution ? {
					id: b.tool_execution.id,
					tool_name: b.tool_execution.tool_name,
					tool_action: b.tool_execution.tool_action,
					status: (status === 'stopped' && b.tool_execution.status === 'running') ? 'stopped' : b.tool_execution.status,
					result_summary: b.tool_execution.result_summary,
					result_json: b.tool_execution.result_json,
					arguments_json: b.tool_execution.arguments_json,
					duration_ms: b.tool_execution.duration_ms,
					created_widget_id: b.tool_execution.created_widget_id,
					created_step_id: b.tool_execution.created_step_id,
					created_widget: b.tool_execution.created_widget,
					created_step: b.tool_execution.created_step
				} : undefined
			})) || []

			// Auto-collapse reasoning for blocks that have content or tool execution
			for (const b of blocks) {
				if ((b.content || b.tool_execution) && !manuallyToggledReasoning.value.has(b.id)) {
					collapsedReasoning.value.add(b.id)
				}
			}
			
			return {
				id: c.id,
				role: c.role as ChatRole,
				status: status,
				prompt: c.prompt,
				completion: c.completion,
				completion_blocks: blocks,
				created_at: c.created_at,
				sigkill: c.sigkill,
				feedback_score: c.feedback_score,
				instruction_suggestions: c.instruction_suggestions,
				knowledge_harness_build: c.knowledge_harness_build || null,
				_loaded_instructions: c.loaded_instructions || undefined,
				files: c.files || [],
				// Fork summary fields
				is_fork_summary: c.is_fork_summary,
				source_report_id: c.source_report_id,
				fork_asset_refs: c.fork_asset_refs,
				// Scheduled prompt tag
				scheduled_prompt_id: c.scheduled_prompt_id || null,
			}
		})
		// Update cursors
		hasMore.value = !!response?.has_more
		cursorBefore.value = response?.next_before || null
        await nextTick()
        safeScrollToBottom()
		if (!skipEstimate) {
			await promptBoxRef.value?.refreshContextEstimate?.()
		}
		await enrichForkedQueries()
		// Auto-expand the latest scheduled completion
		const lastScheduledUser = [...messages.value].reverse().find(m => m.scheduled_prompt_id && m.role === 'user')
		if (lastScheduledUser) {
			expandedScheduledIds.value.add(lastScheduledUser.id)
		}
	} catch (e) {
		console.error('Error loading completions:', e)
	} finally {
		completionsLoaded.value = true
	}
}

// Load previous page (older completions) and prepend while preserving scroll anchor
async function loadPreviousCompletions() {
    if (isLoadingMore.value || !hasMore.value) return
    const container = scrollContainer.value
    if (!container) return
    isLoadingMore.value = true
    const prevHeight = container.scrollHeight
    try {
        const qs = cursorBefore.value ? `&before=${encodeURIComponent(cursorBefore.value)}` : ''
        const { data } = await useMyFetch(`/reports/${report_id}/completions?limit=${pageLimit}${qs}`)
        const response = data.value as any
        const list: any[] = response?.completions || []
        const newItems: ChatMessage[] = list.map((c: any) => {
            let status = c.status as ChatStatus
            if (c.sigkill && status === 'in_progress') status = 'stopped'
            
            const blocks = c.completion_blocks?.map((b: any) => ({
                id: b.id,
                seq: b.seq,
                block_index: b.block_index,
                loop_index: b.loop_index,
                phase: b.phase,
                title: b.title,
                icon: b.icon,
                status: b.status,
                content: b.content,
                reasoning: b.reasoning,
                plan_decision: b.plan_decision,
                tool_execution: b.tool_execution ? {
                    id: b.tool_execution.id,
                    tool_name: b.tool_execution.tool_name,
                    tool_action: b.tool_execution.tool_action,
                    status: (status === 'stopped' && b.tool_execution.status === 'running') ? 'stopped' : b.tool_execution.status,
                    result_summary: b.tool_execution.result_summary,
                    result_json: b.tool_execution.result_json,
                    arguments_json: b.tool_execution.arguments_json,
                    duration_ms: b.tool_execution.duration_ms,
                    created_widget_id: b.tool_execution.created_widget_id,
                    created_step_id: b.tool_execution.created_step_id,
                    created_widget: b.tool_execution.created_widget,
                    created_step: b.tool_execution.created_step
                } : undefined
            })) || []

            // Auto-collapse reasoning for blocks that have content or tool execution
            for (const b of blocks) {
                if ((b.content || b.tool_execution) && !manuallyToggledReasoning.value.has(b.id)) {
                    collapsedReasoning.value.add(b.id)
                }
            }
            
            return {
                id: c.id,
                role: c.role as ChatRole,
                status,
                prompt: c.prompt,
                completion_blocks: blocks,
                created_at: c.created_at,
                sigkill: c.sigkill,
                feedback_score: c.feedback_score,
                instruction_suggestions: c.instruction_suggestions,
                files: c.files || [],
                scheduled_prompt_id: c.scheduled_prompt_id || null,
            }
        })
        // Dedupe by id and prepend
        const existingIds = new Set(messages.value.map(m => m.id))
        const toPrepend = newItems.filter(m => !existingIds.has(m.id))
        if (toPrepend.length > 0) {
            messages.value = [...toPrepend, ...messages.value]
            await nextTick()
            // Keep viewport anchored to previous items
            const newHeight = container.scrollHeight
            container.scrollTop = newHeight - prevHeight
        }
        hasMore.value = !!response?.has_more
        cursorBefore.value = response?.next_before || null
    } catch (e) {
        // keep hasMore as-is on error
    } finally {
        isLoadingMore.value = false
    }
}

function onScroll() {
    const container = scrollContainer.value
    if (!container) return
    // Infinite scroll trigger near top
    if (!isLoadingMore.value && hasMore.value) {
        const thresholdTop = 64
        if (container.scrollTop <= thresholdTop) {
            loadPreviousCompletions()
        }
    }

    // Update bottom proximity and user intent
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight)
    isUserAtBottom.value = distanceFromBottom <= RETURN_TO_BOTTOM_PX

    const isScrollingUp = container.scrollTop < lastScrollTop.value
    // Suppress auto-scroll on any upward scroll, regardless of proximity
    if (isScrollingUp) {
        suppressAutoScroll.value = true
    }
    // Re-enable only when the user returns to within tight bottom threshold
    if (!isScrollingUp && distanceFromBottom <= RETURN_TO_BOTTOM_PX) {
        suppressAutoScroll.value = false
    }
    lastScrollTop.value = container.scrollTop
}

async function loadReport() {
	const { data, error } = await useMyFetch(`/api/reports/${report_id}`)
	if (error.value || !data.value) {
		reportNotFound.value = true
		reportLoaded.value = true
		return
	}
	report.value = data.value
	reportLoaded.value = true
}

async function loadVisualizations() {
	try {
		const { data, error } = await useMyFetch(`/api/queries?report_id=${report_id}`, { method: 'GET' })
		if (error.value) throw error.value
		const queries = Array.isArray(data.value) ? data.value : []
		const list: any[] = []
		for (const q of queries) {
			for (const v of (q?.visualizations || [])) {
				if (v && v.id) list.push(v)
			}
		}
		visualizations.value = list
	} catch (e) {
		visualizations.value = []
	}
}

// Fast dashboard refresh triggered by editor save
async function refreshDashboardFast() {
    try {
        const dash = dashboardRef.value
        if (dash && typeof dash.refreshLayout === 'function') {
            await dash.refreshLayout()
        }
    } catch (e) {
        // noop
    }
}

// Ensure dashboard pane opens only when currently closed
const handleOfficeJsResult = async (event: MessageEvent) => {
    // Only accept messages from the hosting taskpane (same-origin parent).
    // The Excel taskpane is served from the same BOW instance as the report,
    // so cross-origin or same-tab-script posts must be rejected.
    if (event.source !== window.parent) return
    if (event.origin !== window.location.origin) return
    const data = event.data
    if (!data || data.type !== 'officeJsResult') return
    let parsed: any = data.data
    try { if (typeof parsed === 'string') parsed = JSON.parse(parsed) } catch { return }
    if (!parsed || !parsed.id) return
    const { id, completion_id: echoedCompletionId, ...body } = parsed
    // Prefer the echoed completion_id (embedded in the runOfficeJs action by
    // the backend). Falling back to the ref covers older tool calls that
    // dispatched before the echo was added. If both are missing we silently
    // drop — which was the silent-drop bug; warn loudly so it's debuggable.
    const completionId = echoedCompletionId || currentOfficeJsCompletionId.value
    if (!completionId) {
        console.warn('[bow-officejs] dropping result — no completion_id (echoed or ref). tool_call_id=', id)
        return
    }
    try {
        await useMyFetch(`/api/completions/${completionId}/tool-results/${id}`, {
            method: 'POST',
            body,
        })
    } catch (e) {
        console.warn('[bow-officejs] POST officeJsResult failed', { tool_call_id: id, completion_id: completionId, error: e })
    }
}

const markdownAutoDir = ref<{ stop: () => void } | null>(null)

onMounted(() => {
    window.addEventListener('dashboard:ensure_open', () => {
        if (!isSplitScreen.value) toggleSplitScreen()
    })
    window.addEventListener('artifact:open', ((ev: CustomEvent) => {
        handleOpenArtifact({ artifactId: ev.detail?.artifact_id })
    }) as EventListener)
    window.addEventListener('message', handleOfficeJsResult)
    markdownAutoDir.value = useMarkdownAutoDir()
})

// When a tool finishes saving a new step, broadcast the default step change if we have enough info
// Track last dispatched step to avoid duplicate events during streaming
const lastDispatchedStepId = ref<string | null>(null)

watch(
    // Only watch the created step ID, not deep message content
    () => {
        const last = [...messages.value].reverse().find(m => m.role === 'system')
        const lastBlock = last?.completion_blocks?.slice(-1)[0]
        return lastBlock?.tool_execution?.created_step?.id || null
    },
    (stepId) => {
        if (!stepId || stepId === lastDispatchedStepId.value) return
        lastDispatchedStepId.value = stepId
        
        try {
            const last = [...messages.value].reverse().find(m => m.role === 'system')
            const te = last?.completion_blocks?.slice(-1)[0]?.tool_execution as any
            if (te?.created_step?.query_id) {
                window.dispatchEvent(new CustomEvent('query:default_step_changed', {
                    detail: { query_id: te.created_step.query_id, step: te.created_step }
                }))
            }
        } catch {}
    }
)

async function loadActiveLayoutHasBlocks(): Promise<boolean> {
    try {
        const { data } = await useMyFetch(`/api/reports/${report_id}/layouts`)
        const layouts = Array.isArray(data.value) ? (data.value as any[]) : []
        const active = layouts.find((l: any) => l.is_active)
        const result = !!(active && Array.isArray(active.blocks) && active.blocks.length > 0)
        hasLegacyLayout.value = result
        return result
    } catch (e) {
        hasLegacyLayout.value = false
        return false
    }
}

// Check if the report has any artifacts
async function checkHasArtifacts(): Promise<boolean> {
    try {
        const { data } = await useMyFetch(`/artifacts/report/${report_id}`)
        const artifacts = Array.isArray(data.value) ? data.value : []
        reportArtifacts.value = artifacts
        hasArtifacts.value = artifacts.length > 0
        return hasArtifacts.value
    } catch (e) {
        reportArtifacts.value = []
        hasArtifacts.value = false
        return false
    }
}

// Sidebar control (for collapsing when entering split screen)
const { collapse: collapseSidebar } = useSidebar()

function toggleSplitScreen() {
	nextTick(() => {
		isSplitScreen.value = !isSplitScreen.value
		if (isSplitScreen.value) {
			const windowWidth = window.innerWidth
			leftPanelWidth.value = rightPanelView.value === 'summary'
				? Math.round(windowWidth * 0.55)
				: rightPanelView.value === 'agent'
				? Math.round(windowWidth * 0.45)
				: Math.round(windowWidth * 0.37)
			collapseSidebar()
		}
        safeScrollToBottom()
	})
}

function startResize(e: MouseEvent) {
	isResizing.value = true
	initialMouseX.value = e.clientX
	initialPanelWidth.value = leftPanelWidth.value
		document.addEventListener('mousemove', handleResize)
	document.addEventListener('mouseup', stopResize)
	document.body.style.userSelect = 'none'
}

function handleResize(e: MouseEvent) {
	if (!isResizing.value) return
	const minWidth = 280
	const maxWidth = window.innerWidth * 0.8
	const dx = e.clientX - initialMouseX.value
	// Under RTL the chat panel is visually on the right and the resizer sits
	// on its right edge, so a rightward drag shrinks it.
	const newWidth = initialPanelWidth.value + (isRtl.value ? -dx : dx)
	leftPanelWidth.value = Math.min(Math.max(newWidth, minWidth), maxWidth)
	// Trigger scroll to bottom during live resize to maintain scroll position
    safeScrollToBottom()
}

function stopResize() {
	isResizing.value = false
	document.removeEventListener('mousemove', handleResize)
	document.removeEventListener('mouseup', stopResize)
	document.body.style.userSelect = 'auto'
}

onUnmounted(() => {
	if (import.meta.client) {
		window.removeEventListener('resize', checkMobile)
	}
	window.removeEventListener('message', handleOfficeJsResult)
	document.removeEventListener('mousemove', handleResize)
	document.removeEventListener('mouseup', stopResize)
	document.body.style.userSelect = 'auto'
    window.removeEventListener('resize', safeScrollToBottom)
	try { scrollContainer.value?.removeEventListener('scroll', onScroll) } catch {}
	// Cancel any pending animation frame for scroll
	if (scrollRAF !== null && typeof window !== 'undefined') {
		window.cancelAnimationFrame(scrollRAF)
	}
	// Stop any polling timers
	stopPollingInProgressCompletion()
	stopScheduledCompletionsPoll()
	markdownAutoDir.value?.stop()
	// Clear reasoning refs
	reasoningRefs.value.clear()
})


// Handle Add to dashboard from ToolWidgetPreview
async function handleAddWidgetFromPreview(payload: { widget?: any, step?: any, visualization?: any }) {
    try {
        const viz = payload?.visualization
        const widget = payload?.widget
        if (viz?.id) {
            const block = { type: 'visualization', visualization_id: viz.id, x: 0, y: 0, width: 6, height: 7 }
            await useMyFetch(`/api/reports/${report_id}/layouts/active/blocks`, { method: 'PATCH', body: { blocks: [block] } })
        } else if (widget?.id) {
            const block = { type: 'widget', widget_id: widget.id, x: 0, y: 0, width: 6, height: 7 }
            await useMyFetch(`/api/reports/${report_id}/layouts/active/blocks`, { method: 'PATCH', body: { blocks: [block] } })
        } else {
            return
        }
        
        // Update the local widget status immediately to reflect the change in UI
        // Find the tool execution that contains this widget and update its status
        messages.value.forEach(message => {
            if (message.completion_blocks) {
                message.completion_blocks.forEach(block => {
                    if (viz?.id && (block.tool_execution as any)?.created_visualizations) {
                        const list = (block.tool_execution as any).created_visualizations as any[]
                        const found = list.find(v => v?.id === viz.id)
                        if (found) found.status = 'published'
                    }
                    if (widget?.id && block.tool_execution?.created_widget?.id === widget.id && block.tool_execution) {
                        block.tool_execution.created_widget.status = 'published'
                    }
                })
            }
        })
        
        		if (!isSplitScreen.value) toggleSplitScreen()
		await loadVisualizations()
        // Ask dashboard to refresh layout immediately so item appears
        try {
            const dash = dashboardRef.value
            if (dash && typeof dash.refreshLayout === 'function') await dash.refreshLayout()
        } catch {}
		// Scroll to bottom when dashboard opens after adding widget
		await nextTick()
        safeScrollToBottom()
    } catch (e) {
        console.error('Failed to add widget from preview:', e)
    }
}

// Handle opening an artifact from CreateArtifactTool
function handleOpenArtifact(payload: { artifactId?: string; loading?: boolean }) {
	// Switch to artifact view and ensure split screen is open
	if (!isSplitScreen.value) toggleSplitScreen()
	// Switch to artifact panel
	rightPanelView.value = 'artifact'
	// If artifactId provided, dispatch event to ArtifactFrame to select this artifact
	// If loading is true, just open the pane - ArtifactFrame will show loading state
	// and artifact:created event will trigger selection when ready
	if (payload.artifactId) {
		try {
			window.dispatchEvent(new CustomEvent('artifact:select', {
				detail: { artifact_id: payload.artifactId }
			}))
		} catch {}
	}
}

function abortStream() {
	if (currentController) {
		currentController.abort()
		currentController = null
	}
	// Signal backend to stop the running agent loop if we know the server-side id
	try {
					const sysMsg = [...messages.value].reverse().find(m => m.role === 'system' && m.status === 'in_progress')
		const systemId = (sysMsg as any)?.system_completion_id
		if (systemId) {
			useMyFetch(`/api/completions/${systemId}/sigkill`, { method: 'POST' })
			// Mark locally as stopped for immediate UI feedback
			const msgIndex = messages.value.findIndex(m => m.id === sysMsg?.id)
			if (msgIndex !== -1) {
				// Force Vue reactivity by replacing the entire array
				const newMessages = [...messages.value]
				const updatedMessage = { ...newMessages[msgIndex], status: 'stopped' as ChatStatus }
				
				// Also update all completion blocks and their tool executions to stopped status
				if (updatedMessage.completion_blocks) {
					updatedMessage.completion_blocks = updatedMessage.completion_blocks.map(block => ({
						...block,
						status: block.status === 'in_progress' ? 'stopped' as ChatStatus : block.status,
						completed_at: block.completed_at || new Date().toISOString(),
						tool_execution: block.tool_execution?.status === 'running' ? { ...block.tool_execution, status: 'stopped' } : block.tool_execution
					}))
				}
				
				newMessages[msgIndex] = updatedMessage
				messages.value = newMessages
				
				// Force a nextTick update
				nextTick(() => {
				})
			}
		}
	} catch (e) {
		console.error('Failed to send sigkill:', e)
	}
	isStreaming.value = false
	isCompletionInProgress.value = false
}

function openTraceModal(completionId: string) {
	selectedCompletionForTrace.value = completionId
	showTraceModal.value = true
}

function handleExampleClick(starter: string) {
	if (starter) {
		onSubmitCompletion({ text: starter, mentions: [] });
	}
}

// Handlers for feedback-triggered instruction suggestions
function handleSuggestionsLoading(message: ChatMessage) {
	message.instruction_suggestions_loading = true
}

function handleSuggestionsReceived(message: ChatMessage, suggestions: any[]) {
	message.instruction_suggestions_loading = false
	if (suggestions && suggestions.length > 0) {
		// Append new suggestions to existing ones (if any)
		if (!message.instruction_suggestions) {
			message.instruction_suggestions = []
		}
		message.instruction_suggestions.push(...suggestions)
	}
}

// State for QueryCodeEditorModal
const showQueryEditor = ref(false)
const queryEditorProps = ref<{
	queryId: string | null
	stepId: string | null
	initialCode: string
	title: string
}>({
	queryId: null,
	stepId: null,
	initialCode: '',
	title: ''
})

function handleEditQuery(payload: { queryId: string; stepId: string | null; initialCode: string; title: string }) {
	queryEditorProps.value = {
		queryId: payload.queryId,
		stepId: payload.stepId,
		initialCode: payload.initialCode,
		title: payload.title
	}
	showQueryEditor.value = true
}

function closeQueryEditor() {
	showQueryEditor.value = false
}

function onStepCreated(step: any) {
	// Handle step creation - could refresh the current view or update state
	console.log('Step created:', step)
	// Optionally refresh the completion or update the UI
}

function onSubmitCompletion(data: { text: string, mentions: any[]; mode?: string; model_id?: string; files?: { id: string; filename: string; content_type: string }[] }) {
	const text = data.text.trim()
	if (!text) return

	// Append user message with attached files (for immediate display)
	const userMsg: ChatMessage = {
		id: `user-${Date.now()}`,
		role: 'user',
		prompt: { content: text },
		files: data.files || []
	}
	messages.value.push(userMsg)

	// Append placeholder system message for streaming
	const sysId = `system-${Date.now()}`
	const sysMsg: ChatMessage = {
		id: sysId,
		role: 'system',
		status: 'in_progress',
		completion_blocks: []
	}
	messages.value.push(sysMsg)
	scrollToBottom()

	// Stop any background polling and start streaming
	stopPollingInProgressCompletion()

	// Start streaming
	if (isStreaming.value) abortStream()
	currentController = new AbortController()
	isStreaming.value = true
	isCompletionInProgress.value = true

	const requestBody = {
		prompt: {
			content: text,
			mentions: data.mentions || [],
			mode: data.mode || 'chat',
			model_id: data.model_id || null,
			platform: isExcel.value ? 'excel' : null,
			platform_context: isExcel.value && excelSelection.value ? {
				address: excelSelection.value.address,
				sheetName: excelSelection.value.sheetName,
				selectionValues: excelSelection.value.selectionValues,
				cellCount: excelSelection.value.cellCount,
				totalCellCount: excelSelection.value.totalCellCount,
				truncated: excelSelection.value.truncated,
				rowCount: excelSelection.value.rowCount,
				columnCount: excelSelection.value.columnCount,
			} : null,
		},
		stream: true
	}

	startStreaming(requestBody, sysId)
}

async function startStreaming(requestBody: any, sysId: string) {

	try {
		const options: any = {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(requestBody),
			signal: currentController?.signal,
			stream: true
		}
		const raw: any = await useMyFetch(`/reports/${report_id}/completions`, options as any)
		const res: Response = (raw?.data?.value ?? raw?.data) as unknown as Response

		if (!res?.ok || !res?.body) throw new Error(`Stream HTTP error: ${res?.status}`)

		const reader = res.body!.getReader()
		const decoder = new TextDecoder()
		let buffer = ''
		let currentEvent: string | null = null

		const ensureSys = () => messages.value.findIndex(m => m.id === sysId)

		while (true) {
			const { done, value } = await reader.read()
			if (done) {
				break
			}
			
			// Check if stream was aborted
			if (currentController?.signal.aborted) {
				break
			}
			
			buffer += decoder.decode(value, { stream: true })

			let nlIndex: number
			while ((nlIndex = buffer.indexOf('\n')) >= 0) {
				const line = buffer.slice(0, nlIndex).trimEnd()
				buffer = buffer.slice(nlIndex + 1)

				if (line.startsWith('event:')) {
					currentEvent = line.slice(6).trim()
				} else if (line.startsWith('data:')) {
					const dataStr = line.slice(5).trim()
					if (dataStr === '[DONE]') {
						isStreaming.value = false
						currentController = null
						// Refresh report data and context estimate after stream fully ends
						loadReport()
						loadReportSummary()
						promptBoxRef.value?.refreshContextEstimate?.()
						return
					}
					try {
						const parsed = JSON.parse(dataStr)
						const payload = parsed.data ?? parsed
						const idx = ensureSys()
						if (idx !== -1) {
							await handleStreamingEvent(currentEvent, payload, idx)
							// Debounced scroll: batch multiple token events into a single frame
							if (!pendingScroll.value) {
								pendingScroll.value = true
								if (typeof window !== 'undefined') {
									scrollRAF = window.requestAnimationFrame(() => {
										autoScrollIfNearBottom()
										pendingScroll.value = false
									})
								} else {
									autoScrollIfNearBottom()
									pendingScroll.value = false
								}
							}
						}
					} catch (e) {
						// ignore non-JSON data lines
					}
				}
			}
		}
	} catch (err) {
		console.error('Streaming error:', err)
		const idx = messages.value.findIndex(m => m.id === sysId)
		if (idx !== -1) {
			let errorMessage = 'An error occurred during streaming.'
			
			if (err instanceof Error) {
				if (err.name === 'AbortError') {
					// Check if this was a user-initiated stop (sigkill) vs connection abort
					const sysMsg = messages.value[idx]
					// If the main analysis already left 'in_progress', the SSE stream was
					// only still open for the knowledge-harness tail. Aborting it on a new
					// user submit should not downgrade the result to "Generation stopped" —
					// preserve whatever status the user has already seen (success with
					// thumbs up, error, or an existing 'stopped').
					if (sysMsg && sysMsg.status && sysMsg.status !== 'in_progress') {
						return
					}
					if (sysMsg && sysMsg.system_completion_id) {
						// This was likely a user stop, mark as stopped without error
						messages.value[idx] = { ...messages.value[idx], status: 'stopped' }
						return // Don't add error block for user stops
					} else {
						// Connection was aborted for other reasons
						errorMessage = 'Stream was cancelled.'
						messages.value[idx] = { ...messages.value[idx], status: 'stopped' }
					}
				} else if (err.message.includes('Stream HTTP error')) {
					errorMessage = `Connection error: ${err.message}`
					messages.value[idx] = { ...messages.value[idx], status: 'error' }
				} else {
					errorMessage = `Error: ${err.message}`
					messages.value[idx] = { ...messages.value[idx], status: 'error' }
				}
			} else {
				messages.value[idx] = { ...messages.value[idx], status: 'error' }
			}
			
			// Add error block if not already present
			if (!messages.value[idx].completion_blocks?.some(b => b.status === 'error')) {
				if (!messages.value[idx].completion_blocks) {
					messages.value[idx].completion_blocks = []
				}
				messages.value[idx].completion_blocks!.push({
					id: `error-${Date.now()}`,
					block_index: 999,
					status: 'error',
					content: errorMessage,
					title: 'Error',
					icon: '❌'
				})
			}
		}
	} finally {
		isStreaming.value = false
		isCompletionInProgress.value = false
		currentController = null
	}
}

// === Minimal polling for refresh resume (no SSE resume) ===
const isPolling = ref<boolean>(false)
const pollIntervalMs = 1200
let pollHandle: number | null = null

function getLastInProgressSystem(): ChatMessage | undefined {
	return [...messages.value].reverse().find(m => m.role === 'system' && m.status === 'in_progress')
}

function stopPollingInProgressCompletion() {
	if (pollHandle !== null) {
		clearTimeout(pollHandle)
		pollHandle = null
	}
	isPolling.value = false
}

async function startPollingInProgressCompletion() {
	if (isStreaming.value || isPolling.value) return
	const sys = getLastInProgressSystem()
	if (!sys) return

	isPolling.value = true
	const startTs = Date.now()
	const maxDurationMs = 2 * 60 * 1000

	const tick = async () => {
		// If SSE streaming has (re)started, stop polling — SSE is the source of truth
		// and loadCompletions would wipe in-memory stream state.
		if (isStreaming.value) {
			stopPollingInProgressCompletion()
			return
		}
		try {
			await loadCompletions({ skipEstimate: true })
			autoScrollIfNearBottom()
			const still = getLastInProgressSystem()
			if (!still) {
				stopPollingInProgressCompletion()
				promptBoxRef.value?.refreshContextEstimate?.()
				return
			}
			if (Date.now() - startTs > maxDurationMs) {
				stopPollingInProgressCompletion()
				return
			}
			// Schedule next tick only if we should continue polling
			pollHandle = window.setTimeout(tick, pollIntervalMs)
		} catch (e) {
			// keep polling on transient errors
			pollHandle = window.setTimeout(tick, pollIntervalMs)
		}
	}

	pollHandle = window.setTimeout(tick, pollIntervalMs)
}

// === Background poll to detect new scheduled completions ===
let scheduledPollHandle: number | null = null
const scheduledPollIntervalMs = 15_000

function startScheduledCompletionsPoll() {
	if (scheduledPollHandle !== null) return
	// Only poll if this report actually has scheduled prompts that can fire in the background
	if (!scheduledPrompts.value || scheduledPrompts.value.length === 0) return
	const tick = async () => {
		// Skip while streaming (SSE is authoritative) or while tab is hidden
		if (isStreaming.value || (typeof document !== 'undefined' && document.hidden)) {
			scheduledPollHandle = window.setTimeout(tick, scheduledPollIntervalMs)
			return
		}
		try {
			const lastId = messages.value.length > 0 ? messages.value[messages.value.length - 1].id : null
			const { data } = await useMyFetch(`/reports/${report_id}/completions?limit=${pageLimit}`)
			const response = data.value as any
			const list: any[] = response?.completions || []
			const newLastId = list.length > 0 ? list[list.length - 1].id : null
			if (newLastId && newLastId !== lastId) {
				await loadCompletions()
				autoScrollIfNearBottom()
			}
		} catch {}
		scheduledPollHandle = window.setTimeout(tick, scheduledPollIntervalMs)
	}
	scheduledPollHandle = window.setTimeout(tick, scheduledPollIntervalMs)
}

function stopScheduledCompletionsPoll() {
	if (scheduledPollHandle !== null) {
		clearTimeout(scheduledPollHandle)
		scheduledPollHandle = null
	}
}

onMounted(async () => {
	// Load report metadata first (fast), then open sidebar based on counts
	// loadCompletions is slow (~30s) so don't block sidebar on it
	const fastLoads = Promise.all([
		loadReport(),
		loadVisualizations(),
		checkHasArtifacts(),
		loadActiveLayoutHasBlocks(),
		loadScheduledPrompts(),
		loadReportSummary()
	])
	const slowLoads = loadCompletions()

	await fastLoads

	// Auto-open right pane based on report metadata (available immediately from loadReport)
	// Skip auto-open in Excel mode — the taskpane is too narrow for split screen
	if (!isExcel.value) {
		if (hasArtifacts.value || hasLegacyLayout.value || (report.value as any)?.artifact_count > 0) {
			isSplitScreen.value = true
			rightPanelView.value = 'artifact'
			leftPanelWidth.value = Math.round(window.innerWidth * 0.37)
			collapseSidebar()
		} else if ((report.value as any)?.query_count > 0 || (report.value as any)?.instruction_count > 0 || (report.value as any)?.has_scheduled_prompts) {
			isSplitScreen.value = true
			rightPanelView.value = 'summary'
			leftPanelWidth.value = Math.round(window.innerWidth * 0.55)
		}
	}

	await slowLoads

	// Handle new_message query parameter after everything is loaded
	if (route.query.new_message && messages.value.length == 0) {
		let mentions: any[] = []
		try {
			const raw = typeof route.query.mentions === 'string' ? decodeURIComponent(route.query.mentions) : ''
			if (raw) mentions = JSON.parse(raw)
		} catch {}
		const mode = typeof route.query.mode === 'string' ? route.query.mode : 'chat'
		const model_id = typeof route.query.model_id === 'string' ? route.query.model_id : null
		onSubmitCompletion({ text: route.query.new_message as string, mentions, mode, model_id: model_id || undefined })
	}

	// If a system message is still in progress (after refresh), begin polling until it finishes
	if (!isStreaming.value && getLastInProgressSystem()) {
		startPollingInProgressCompletion()
	}

	// Start background poll for new scheduled completions
	startScheduledCompletionsPoll()
	
    // Aggressive initial scroll to handle async content mounting
	scheduleInitialScroll()
    window.addEventListener('resize', safeScrollToBottom)
	// Attach scroll listener for infinite scroll up
	try { scrollContainer.value?.addEventListener('scroll', onScroll) } catch {}
    // Initialize scroll position state
    try {
        const c = scrollContainer.value
        if (c) {
            lastScrollTop.value = c.scrollTop
            const dist = c.scrollHeight - (c.scrollTop + c.clientHeight)
            isUserAtBottom.value = dist <= RETURN_TO_BOTTOM_PX
            suppressAutoScroll.value = false
        }
    } catch {}
})

</script>

<style scoped>
.overflow-y-auto {
	overflow-y: auto !important;
}

/* Thinking box - collapsible reasoning */
.thinking-box {
	margin-bottom: 4px;
}

.thinking-header {
	display: flex;
	align-items: center;
	cursor: pointer;
	font-size: 12px;
	font-weight: 400;
	color: #6b7280;
	user-select: none;
}

.thinking-header:hover {
	color: #374151;
}

.thinking-content {
	padding-block: 4px;
	padding-inline-start: 10px;
	padding-inline-end: 0;
	margin-top: 2px;
	margin-bottom: 4px;
	border-inline-start: 1px dashed #e5e7eb;
	font-size: 12px !important;
	line-height: 1.4;
	color: #6b7280;
}

.thinking-content :deep(*) {
	font-size: 12px !important;
	line-height: 1.4 !important;
}

.thinking-content :deep(.markdown-content) {
	font-size: 12px !important;
	line-height: 1.4 !important;
}

.thinking-content :deep(p) {
	font-size: 12px !important;
	margin: 0;
}

/* Tool execution - clear visual separation */
.tool-execution-container {
	margin: 8px 0;
}

/* Block content - assistant messages */
.block-content {
	margin-bottom: 4px;
	font-size: 13px;
}

/* Minimal typography akin to CompletionMessageComponent */
.markdown-wrapper :deep(.markdown-content) {
	@apply leading-relaxed;
	font-size: 13px;
	/* Prevent layout thrashing during streaming */
	contain: content;
	content-visibility: auto;

	/* Paragraph spacing to match streaming text appearance */
	p {
		margin-bottom: 1em;
		unicode-bidi: plaintext;
	}
	p:last-child {
		margin-bottom: 0;
	}

	:where(h1, h2, h3, h4, h5, h6) {
		@apply font-bold mb-4 mt-6;
		unicode-bidi: plaintext;
	}

	h1 { @apply text-2xl; }
	h2 { @apply text-xl; }
	h3 { @apply text-lg; }

	ul, ol { @apply ps-6 mb-4; unicode-bidi: plaintext; }
	ul { @apply list-disc; }
	ol { @apply list-decimal; }
	li { @apply mb-1.5; unicode-bidi: plaintext; }

	/* Code blocks (fenced with ```) — always LTR regardless of surrounding direction */
	pre {
		@apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto;
		white-space: pre-wrap;
		word-wrap: break-word;
		direction: ltr;
		unicode-bidi: isolate;
		text-align: left;
	}
	pre code {
		/* Reset inline code styles for code blocks */
		background: none;
		padding: 0;
		border-radius: 0;
		font-size: 13px;
		line-height: 1.5;
		display: block;
		white-space: pre-wrap;
		word-wrap: break-word;
	}
	/* Inline code (single backticks) */
	code {
		@apply bg-gray-100 px-1.5 py-0.5 rounded font-mono;
		font-size: 12px;
		color: #374151;
		unicode-bidi: isolate;
		direction: ltr;
	}
	a { 
		@apply text-gray-900 no-underline relative;
		transition: color 0.15s ease;
	}
	a:hover {
		@apply text-gray-700;
	}
	a::before {
		content: '';
		position: absolute;
		left: -18px;
		top: 50%;
		transform: translateY(-50%);
		width: 14px;
		height: 14px;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke-width='1.5' stroke='%236b7280'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' d='M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25'/%3E%3C/svg%3E");
		background-size: contain;
		background-repeat: no-repeat;
		opacity: 0;
		transition: opacity 0.15s ease;
	}
	a:hover::before {
		opacity: 1;
	}
	blockquote { @apply border-l-4 border-gray-200 pl-4 italic my-4; unicode-bidi: plaintext; }
	table { @apply w-full border-collapse mb-4; unicode-bidi: plaintext; }
	table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; unicode-bidi: plaintext; }
}



/* Compact mode (Excel add-in) — smaller text throughout */
.compact-messages .block-content {
	font-size: 13px;
}
.compact-messages .markdown-wrapper :deep(.markdown-content) {
	font-size: 13px;
}
.compact-messages .markdown-wrapper :deep(.markdown-content pre code) {
	font-size: 12px;
}
.compact-messages .markdown-wrapper :deep(.markdown-content code) {
	font-size: 12px;
}
.compact-messages .thinking-header {
	font-size: 11px;
}
.compact-messages .thinking-content,
.compact-messages .thinking-content :deep(*),
.compact-messages .thinking-content :deep(.markdown-content),
.compact-messages .thinking-content :deep(p) {
	font-size: 11px !important;
}
.compact-messages li {
	font-size: 13px;
}

@keyframes simple-ellipsis { 0% { content: '.'; } 33% { content: '..'; } 66% { content: '...'; } }
.simple-dots::after { content: '.'; display: inline-block; margin-top: 5px; animation: simple-ellipsis 1.5s infinite; font-weight: 400; font-size: 14px; color: #888; }

@keyframes shimmer {
	0% { background-position: -100% 0; }
	100% { background-position: 100% 0; }
}

@keyframes ellipsis {
	0% { content: 'Thinking.'; }
	33% { content: 'Thinking..'; }
	66% { content: 'Thinking...'; }
}

.dots::after {
	content: 'Thinking...';
	display: inline-block;
	background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
	background-size: 200% 100%;
	-webkit-background-clip: text;
	background-clip: text;
	color: transparent;
	animation: shimmer 2s linear infinite, ellipsis 1s infinite;
	font-weight: 400;
	font-size: 12px;
	opacity: 1;
}

/* Add fade transitions */
.fade-enter-active,
.fade-leave-active {
	transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
	opacity: 0;
}

.fade-in {
    animation: fadeIn 0.6s ease-in;
}

@keyframes fadeIn {
    0% {
        opacity: 0;
        transform: translateY(10px);
    }
    100% {
        opacity: 1;
        transform: translateY(0);
    }
}

/* Minimal shimmer for reconnect banner */
.poll-shimmer {
	background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
	background-size: 200% 100%;
	-webkit-background-clip: text;
	background-clip: text;
	color: transparent;
	animation: shimmer 2s linear infinite;
	font-weight: 400;
	opacity: 1;
}
</style>



