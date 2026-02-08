/**
 * Shared helper functions for instruction display
 */

export interface InstructionLabel {
  id: string
  name: string
  color?: string | null
  description?: string | null
}

export interface DataSource {
  id: string
  name: string
  type: string
}

export interface User {
  id: string
  name: string
  email?: string
}

export interface Instruction {
  id: string
  text: string
  title?: string | null
  thumbs_up?: number
  status: 'draft' | 'published' | 'archived'
  category: 'code_gen' | 'data_modeling' | 'general' | 'system' | 'visualizations' | 'dashboard'
  user_id?: string | null
  organization_id: string
  user?: User | null
  data_sources: DataSource[]
  created_at: string
  updated_at: string
  
  // DEPRECATED: Dual-status lifecycle fields (approval workflow moved to builds)
  private_status?: string | null   // DEPRECATED - not used
  global_status?: string | null    // DEPRECATED - not used
  is_seen?: boolean
  can_user_toggle?: boolean
  reviewed_by_user_id?: string | null  // DEPRECATED - not used
  reviewed_by?: User | null            // DEPRECATED - not used
  labels?: InstructionLabel[]
  
  // Unified Instructions System fields
  source_type?: 'user' | 'ai' | 'git'
  source_metadata_resource_id?: string | null
  source_git_commit_sha?: string | null
  source_sync_enabled?: boolean
  load_mode?: 'always' | 'intelligent' | 'disabled'
  structured_data?: Record<string, any> | null
  formatted_content?: string | null
  references?: any[]
}

export function useInstructionHelpers() {
  // Status helpers
  const formatStatus = (status: string) => {
    const statusMap: Record<string, string> = {
      draft: 'Draft',
      published: 'Published',
      archived: 'Archived'
    }
    return statusMap[status] || status
  }

  // SIMPLIFIED: Status helpers now only use instruction.status
  // Approval workflow is handled by builds, not instruction status
  const getStatusClass = (instruction: Instruction) => {
    const statusClasses: Record<string, string> = {
      draft: 'bg-yellow-100 text-yellow-800',
      published: 'bg-green-100 text-green-800',
      archived: 'bg-gray-100 text-gray-800'
    }
    return statusClasses[instruction.status] || 'bg-gray-100 text-gray-800'
  }

  const getStatusIconClass = (instruction: Instruction) => {
    const statusClasses: Record<string, string> = {
      draft: 'bg-yellow-400',
      published: 'bg-green-400',
      archived: 'bg-gray-400'
    }
    return statusClasses[instruction.status] || 'bg-gray-400'
  }

  const getStatusTooltip = (instruction: Instruction) => {
    return formatStatus(instruction.status)
  }

  // DEPRECATED: No longer used - kept for backward compatibility
  const getSubStatus = (_instruction: Instruction) => {
    return null
  }

  // Category helpers
  const formatCategory = (category: string) => {
    const categoryMap: Record<string, string> = {
      code_gen: 'Code Generation',
      data_modeling: 'Data Modeling',
      general: 'General',
      system: 'System',
      visualizations: 'Visualizations',
      dashboard: 'Dashboard'
    }
    return categoryMap[category] || category
  }

  const getCategoryIcon = (category: string) => {
    const categoryIcons: Record<string, string> = {
      code_gen: 'heroicons:code-bracket',
      data_modeling: 'heroicons:cube',
      general: 'heroicons:document-text',
      system: 'heroicons:cog-6-tooth',
      visualizations: 'heroicons:chart-bar',
      dashboard: 'heroicons:squares-2x2'
    }
    return categoryIcons[category] || 'heroicons:document-text'
  }

  const getCategoryClass = (category: string) => {
    const categoryClasses: Record<string, string> = {
      code_gen: 'bg-purple-100 text-purple-800',
      code: 'bg-purple-100 text-purple-800',
      data_modeling: 'bg-blue-100 text-blue-800',
      general: 'bg-gray-100 text-gray-800',
      system: 'bg-orange-100 text-orange-800',
      visualizations: 'bg-teal-100 text-teal-800',
      dashboard: 'bg-indigo-100 text-indigo-800'
    }
    return categoryClasses[category] || 'bg-gray-100 text-gray-800'
  }

  // Source type helpers
  const getSourceType = (instruction: Instruction) => {
    if (instruction.source_type) return instruction.source_type
    if (instruction.source_metadata_resource_id) return 'git'
    return 'user'
  }

  const getSourceIcon = (instruction: Instruction) => {
    const sourceType = getSourceType(instruction)
    const icons: Record<string, string> = {
      git: 'i-heroicons-code-bracket',
      ai: 'i-heroicons-sparkles',
      user: 'i-heroicons-user'
    }
    return icons[sourceType] || 'i-heroicons-user'
  }

  const getSourceTooltip = (instruction: Instruction) => {
    const sourceType = getSourceType(instruction)
    const tooltips: Record<string, string> = {
      git: 'From Git Repository',
      ai: 'AI Generated',
      user: 'User Created'
    }
    return tooltips[sourceType] || 'User Created'
  }

  // Resource type helpers (for git-sourced instructions)
  const getResourceType = (instruction: Instruction): string | null => {
    if (!instruction.structured_data) return null
    return instruction.structured_data.resource_type || null
  }

  /**
   * Get the icon path for a resource type (image-based icons)
   * Returns null if should use heroicons instead
   */
  const getResourceTypeIcon = (instruction: Instruction): string | null => {
    const resourceType = getResourceType(instruction)
    if (!resourceType) return null

    // Map resource types to icon files
    const iconMap: Record<string, string> = {
      // dbt
      'dbt_model': '/icons/dbt.png',
      'dbt_metric': '/icons/dbt.png',
      'dbt_source': '/icons/dbt.png',
      'dbt_seed': '/icons/dbt.png',
      'dbt_macro': '/icons/dbt.png',
      'dbt_test': '/icons/dbt.png',
      'dbt_exposure': '/icons/dbt.png',
      'model': '/icons/dbt.png',
      'model_config': '/icons/dbt.png',
      'metric': '/icons/dbt.png',
      // LookML
      'lookml_view': '/icons/lookml.png',
      'lookml_model': '/icons/lookml.png',
      'lookml_explore': '/icons/lookml.png',
      // Markdown
      'markdown_document': '/icons/markdown.png',
      'markdown': '/icons/markdown.png',
      // Tableau
      'tableau_workbook': '/icons/tableau.png',
      'tableau_dashboard': '/icons/tableau.png',
      'tableau_view': '/icons/tableau.png',
      // Dataform
      'dataform_table': '/icons/dataform.png',
      'dataform_view': '/icons/dataform.png',
      'dataform_incremental': '/icons/dataform.png',
      // Snowflake semantic
      'snowflake_semantic_view': '/data_sources_icons/snowflake.png',
      // Generic file types
      'sql_file': '/icons/resource.png',
      'yaml_file': '/icons/resource.png',
      'generic_file': '/icons/resource.png',
    }

    return iconMap[resourceType] || null
  }

  /**
   * Get tooltip text for resource type
   */
  const getResourceTypeTooltip = (instruction: Instruction): string => {
    const resourceType = getResourceType(instruction)
    if (!resourceType) return 'Unknown'

    const tooltipMap: Record<string, string> = {
      // dbt
      'dbt_model': 'dbt Model',
      'dbt_metric': 'dbt Metric',
      'dbt_source': 'dbt Source',
      'dbt_seed': 'dbt Seed',
      'dbt_macro': 'dbt Macro',
      'dbt_test': 'dbt Test',
      'dbt_exposure': 'dbt Exposure',
      'model': 'dbt Model',
      'model_config': 'dbt Model Config',
      'metric': 'Metric',
      // LookML
      'lookml_view': 'LookML View',
      'lookml_model': 'LookML Model',
      'lookml_explore': 'LookML Explore',
      // Markdown
      'markdown_document': 'Markdown Document',
      'markdown': 'Markdown',
      // Tableau
      'tableau_workbook': 'Tableau Workbook',
      'tableau_dashboard': 'Tableau Dashboard',
      'tableau_view': 'Tableau View',
      // Dataform
      'dataform_table': 'Dataform Table',
      'dataform_view': 'Dataform View',
      'dataform_incremental': 'Dataform Incremental',
      // Snowflake semantic
      'snowflake_semantic_view': 'Snowflake Semantic View',
      // Generic file types
      'sql_file': 'SQL File',
      'yaml_file': 'YAML File',
      'generic_file': 'File',
    }

    return tooltipMap[resourceType] || resourceType.replace(/_/g, ' ')
  }

  /**
   * Get heroicon name as fallback when no image icon exists
   */
  const getResourceTypeFallbackIcon = (instruction: Instruction): string => {
    const resourceType = getResourceType(instruction)
    if (!resourceType) return 'i-heroicons-document-text'

    // Fallback heroicons by category
    if (resourceType.startsWith('dbt_') || resourceType === 'model' || resourceType === 'model_config' || resourceType === 'metric') {
      return 'i-heroicons-cube'
    }
    if (resourceType.startsWith('lookml_')) {
      return 'i-heroicons-eye'
    }
    if (resourceType.startsWith('tableau_')) {
      return 'i-heroicons-chart-bar'
    }
    if (resourceType.startsWith('dataform_')) {
      return 'i-heroicons-table-cells'
    }
    if (resourceType.includes('markdown')) {
      return 'i-heroicons-document-text'
    }
    if (resourceType === 'snowflake_semantic_view') {
      return 'i-heroicons-circle-stack'
    }
    if (resourceType === 'sql_file') {
      return 'i-heroicons-command-line'
    }
    if (resourceType === 'yaml_file') {
      return 'i-heroicons-document-text'
    }

    return 'i-heroicons-document-text'
  }

  // Load mode helpers
  const getLoadModeLabel = (loadMode?: string) => {
    const labels: Record<string, string> = {
      always: 'Always',
      intelligent: 'Smart',
      disabled: 'Off'
    }
    return labels[loadMode || 'always'] || 'Always'
  }

  const getLoadModeClass = (loadMode?: string) => {
    const classes: Record<string, string> = {
      always: 'bg-blue-100 text-blue-700',
      intelligent: 'bg-purple-100 text-purple-700',
      disabled: 'bg-gray-100 text-gray-500'
    }
    return classes[loadMode || 'always'] || 'bg-blue-100 text-blue-700'
  }

  // Data source helpers
  const getDataSourceTooltip = (instruction: Instruction) => {
    if (instruction.data_sources.length === 0) {
      return 'All Data Sources'
    } else if (instruction.data_sources.length === 1) {
      return instruction.data_sources[0].name
    } else {
      return `${instruction.data_sources.length} Data Sources: ${instruction.data_sources.map(ds => ds.name).join(', ')}`
    }
  }

  // Reference helpers
  const getRefIcon = (type: string) => {
    if (type === 'metadata_resource') return 'i-heroicons-rectangle-stack'
    if (type === 'datasource_table') return 'i-heroicons-table-cells'
    if (type === 'memory') return 'i-heroicons-book-open'
    return 'i-heroicons-circle'
  }

  const getRefDisplayName = (ref: any) => {
    const objectType = ref.object_type
    const dataSourceName = ref.data_source_name || 'Unknown'
    if (ref.display_text) return `${dataSourceName} - ${objectType}: ${ref.display_text}`
    if (ref.object?.name) return `${dataSourceName} - ${objectType}: ${ref.object.name}`
    if (ref.object?.title) return `${dataSourceName} - ${objectType}: ${ref.object.title}`
    return `${dataSourceName} - ${objectType}`
  }

  return {
    // Status
    formatStatus,
    getStatusClass,
    getStatusIconClass,
    getStatusTooltip,
    getSubStatus,
    // Category
    formatCategory,
    getCategoryIcon,
    getCategoryClass,
    // Source
    getSourceType,
    getSourceIcon,
    getSourceTooltip,
    // Resource type (for git-sourced)
    getResourceType,
    getResourceTypeIcon,
    getResourceTypeTooltip,
    getResourceTypeFallbackIcon,
    // Load mode
    getLoadModeLabel,
    getLoadModeClass,
    // Data source
    getDataSourceTooltip,
    // References
    getRefIcon,
    getRefDisplayName
  }
}
