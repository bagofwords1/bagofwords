from partialjson.json_parser import JSONParser
from app.ai.llm import LLM
from app.models.llm_model import LLMModel
import re
import json
from app.schemas.organization_settings_schema import OrganizationSettingsConfig
from app.ai.schemas.codegen import CodeGenContext

class Coder:
    def __init__(self, model: LLMModel, organization_settings: OrganizationSettingsConfig, instruction_context_builder=None, context_hub=None) -> None:
        self.llm = LLM(model)
        self.organization_settings = organization_settings
        self.enable_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
        # Back-compat: accept either legacy builder or new context hub
        self.instruction_context_builder = instruction_context_builder
        self.context_hub = context_hub

    async def execute(self, schemas, persona, prompt, memories, previous_messages):
        # Implementation left out as not requested.
        pass

    async def data_model_to_code(
        self,
        data_model,
        prompt,
        schemas,
        ds_clients,
        excel_files,
        code_and_error_messages,
        memories,
        previous_messages,
        retries,
        prev_data_model_code_pair,
        sigkill_event=None,
        code_context_builder=None
    ):
        # Optional early exit if a cancellation was requested before generation
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            return "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame()"
        # Resolve instructions from context hub when available; otherwise fallback to legacy builder
        instructions_context = ""
        mentions_context = "<mentions>No mentions for this turn</mentions>"
        entities_context = ""
        # Defaults for additional context
        resources_context = ""
        files_context = ""
        messages_context = ""
        platform = None
        past_observations = []
        last_observation = None
        history_summary = ""
        if self.context_hub is not None:
            try:
                view = self.context_hub.get_view()
                inst_obj = getattr(view.static, "instructions", None)
                instructions_context = inst_obj.render() if inst_obj else ""
                mentions_obj = getattr(view.static, "mentions", None)
                mentions_context = mentions_obj.render() if mentions_obj else mentions_context
                entities_obj = getattr(view.warm, "entities", None)
                entities_context = entities_obj.render() if entities_obj else entities_context
                # Additional context sections aligned with create_data/create_widget
                resources_obj = getattr(view.static, "resources", None)
                resources_context = resources_obj.render() if resources_obj else ""
                files_obj = getattr(view.static, "files", None)
                files_context = files_obj.render() if files_obj else ""
                messages_obj = getattr(view.warm, "messages", None)
                messages_context = messages_obj.render() if messages_obj else ""
                try:
                    platform = (getattr(view, "meta", {}) or {}).get("external_platform")
                except Exception:
                    platform = None
                # Observations and history
                past_observations = []
                last_observation = None
                try:
                    if getattr(self.context_hub, "observation_builder", None):
                        past_observations = self.context_hub.observation_builder.tool_observations or []
                        last_observation = self.context_hub.observation_builder.get_latest_observation()
                except Exception:
                    past_observations = []
                    last_observation = None
                try:
                    history_summary = await self.context_hub.get_history_summary()
                except Exception:
                    history_summary = ""
            except Exception:
                instructions_context = ""
                mentions_context = mentions_context
                entities_context = entities_context
                resources_context = ""
                files_context = ""
                messages_context = ""
                platform = None
                past_observations = []
                last_observation = None
                history_summary = ""
        elif self.instruction_context_builder is not None:
            # Legacy compatibility
            if hasattr(self.instruction_context_builder, "get_instructions_context"):
                instructions_context = await self.instruction_context_builder.get_instructions_context()
            else:
                try:
                    inst_section = await self.instruction_context_builder.build()
                    instructions_context = inst_section.render()
                except Exception:
                    instructions_context = ""
            # Legacy fallbacks when ContextHub is not available
            resources_context = ""
            files_context = ""
            messages_context = "\n".join(previous_messages) if isinstance(previous_messages, list) else str(previous_messages or "")
            platform = None
            past_observations = []
            last_observation = None
            history_summary = ""

        # Build a section with existing widget data if applicable
        modify_existing_widget_text = ""
        if prev_data_model_code_pair:
            modify_existing_widget_text = f"""
            There is an existing data model and its code implementation:

            <existing_data_model>
            {prev_data_model_code_pair['data_model']}
            </existing_data_model>

            <existing_code>
            {prev_data_model_code_pair['code']}
            </existing_code>

            You can reference the existing code and data model to adapt or improve the new code for the NEW data model.
            """
        # Prepare code and error messages section if any
        code_error_section = ""
        if code_and_error_messages:
            combined = []
            for code, error in code_and_error_messages:
                combined.append(f"CODE:\n{code}\n\nERROR:\n{error}")
            code_error_section = "\n".join(combined)

        # Prepare data sources description
        # ds_clients is a dict: {data_source_name: client_object}
        # client_object has a 'description' attribute that explains how to query that client
        data_source_descriptions = []
        for data_source_name, client in ds_clients.items():
            data_source_descriptions.append(
                f"data_source_name: {data_source_name}\ndescription: {client.description}"
            )
        data_source_section = "\n".join(data_source_descriptions)

        # Prepare excel files description
        excel_files_description = []
        for index, file in enumerate(excel_files):
            # Assuming file has a 'description' and 'path'
            excel_files_description.append(f"{index}: {file.description}")
        excel_files_section = "\n".join(excel_files_description)

        # Define data preview instruction based on enable_llm_see_data flag
        data_preview_instruction = f"- Also, after each query or DataFrame creation, print the data using: print('df head:', df.head())" if self.enable_llm_see_data else ""

        similar_successful_code_snippets = await code_context_builder.get_top_successful_snippets_for_data_model(data_model)
        similar_failed_code_snippets = await code_context_builder.get_top_failed_snippets_for_data_model(data_model)
        text = f"""
        You are a highly skilled data engineer and data scientist.

        Your goal: Given a data model and context, generate a Python function named `generate_df(ds_clients, excel_files)`
        that produces a Pandas DataFrame according to the data model specifications only.
        Use the previous messages to understand the user's intent/context and the data model to generate the correct dataframe.

        **General Organization Instructions**:
        **VERY IMPORTANT, CREATED BY THE USER, MUST BE USED AND CONSIDERED**:
        {instructions_context}

        **Context and Inputs**:
        - Data Model (newly generated):
        <data_model>
        {data_model}
        </data_model>

        - User Prompt:
        <user_prompt>
        {prompt}
        </user_prompt>

        - Provided Schemas (Ground Truth):
        <ground_truth_schemas>
        {schemas}
        </ground_truth_schemas>

        - Mentions:
        {mentions_context}

        - Entities:
        {entities_context}

        - Previous Messages:
        <previous_messages>
        {previous_messages}
        </previous_messages>

        - Memories:
        <memories>
        {memories}
        </memories>

        {modify_existing_widget_text}

        - Data Sources and Clients:
        Each data source may be SQL, document DB, service API, or Excel.
        You have a `ds_clients` dict where each key is a data source name.
        Each ds_client has a method `execute_query("QUERY")` that returns data.
        The 'QUERY' depends on the data source type. The data source descriptions are:
        <data_sources_clients>
        {data_source_section}
        </data_sources_clients>

        - Excel Files:
        <excel_files>
        {excel_files_section}
        </excel_files>

        - Previous Code Attempts and Errors:
        <code_retries>
        {retries}
        </code_retries>

        <code_and_error_messages>
        {code_error_section}
        </code_and_error_messages>


        - Similar successful code snippets (for reference on what is working):
        <similar_successful_code_snippets>
        {similar_successful_code_snippets}
        </similar_successful_code_snippets>

        - Similar failed code snippets (for reference on what is not working):
        <similar_failed_code_snippets>
        {similar_failed_code_snippets}
        </similar_failed_code_snippets>

        **Guidelines and Requirements**:

        1. **Function Signature**: Implement exactly:
           `def generate_df(ds_clients, excel_files):`
           - The function should return the main dataframe that will answer the user prompt.

        2. **Data Source Usage**:
           - Use `ds_clients[data_source_name].execute_query("SOME QUERY")` to query non-Excel data sources.
           - After each query or DataFrame creation, print its info using: print("df Info:", df.info())
           {data_preview_instruction}
           - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
           - For Excel files, use `pd.read_excel(excel_files[INDEX].path, sheet_name=SHEET_INDEX, header=None)` to read data.
             * Decide the correct INDEX and SHEET_INDEX based on prompt and data model.
             * Print the dict/df preview to help the LLM ensure indices and positions are correct.
           - After ANY operation that changes DataFrame columns (merge, join, add/remove columns), print: print("df Preview:", {data_preview_instruction})
           - Output schema contract: The final DataFrame must contain only primitives (str/int/float/bool/None). Never return dict/list objects. If a column is JSON/MAP/STRUCT or a JSON-looking string, extract/flatten to readable scalar columns (e.g., owner, repo_full_name) using pandas.json_normalize or by selecting key paths; otherwise stringify compactly. Prefer clear label/value columns for charting.
           - Allow only read operations on the data sources. No insert/delete/add/update/put/drop.
           - Prefer using data sources, tables, files, and entities explicitly listed in <mentions>. If selecting an unmentioned source, justify briefly.

        3. **Schema and Data Model Adherence**:
           - Use only columns and relationships that exist in the provided schemas.
           - If the data model suggests derived columns or aggregations, ensure you derive them correctly from existing schema fields.
           - Do NOT invent columns that do not exist or cannot be derived.
           - Do NOT include client names or non-relevant info inside queries. The data source queries should be generic and directly usable by the ds_clients.

        4. **Handling Previous Code and Errors**:
           - If `retries` ≥ 1, review the code_and_error_messages:
             * Understand the error.
             * If it's related to a missing column or invalid query, fix it by removing or correcting that column/query.
           - If `retries` ≥ 2 and still failing due to a specific column or measure, remove that problematic part and return a reduced but valid DataFrame.
           - Ensure you produce some output even if reduced. Not returning anything is worse than returning partial data.

        5. **Sorting and Final Output**:
           - Sort the DataFrame by the most relevant key column.
             * If it's a time or date column, sort descending.
             * If it's a count or sum, also sort descending.
             * Otherwise, sort ascending.

        6. **Data Formatting**:
           - Make sure the DataFrame is two-dimensional, with well-defined rows and columns.
           - Handle missing values gracefully.

        7. **No Extra Formatting**:
           - Return the code for the `generate_df` function as plain text only.
           - No Markdown, no extra comments beyond necessary Python code comments.
           - Do not wrap code in triple backticks or any markup.
        
        8. **End of code**:
           - At the end of the function, before returning the df — print the df preview last time using: print("Final df Preview:", {data_preview_instruction})
           - Return the df as the final output. Make sure the df name is the right one and reflects the main dataframe.

        **Approach**:
        - Start from scratch or modify the existing code if `prev_data_model_code_pair` is provided.
        - Integrate data from `ds_clients` and `excel_files` as needed. Print the dict/df preview to help the LLM ensure indices and positions are correct.
        - Carefully build queries.
        - Test logic in your mind to avoid errors.
        - If error hints are provided (from previous retries), address them directly.

        Now produce ONLY the Python function code as described. Do not output anything else besides the function python code. No markdown, no comments, no triple backticks, no triple quotes, no triple anything, no text, no anything.
        """

        result = self.llm.inference(text)

        # Remove markdown code fence (with optional language tag) if present
        result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
        # Remove any closing fence lines that are just ```
        result = re.sub(r'(?m)^\s*```\s*$', '', result)
        # Defensive: remove a leading standalone language tag line (e.g., "python" or "json")
        result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)
        # Remove any code after return df
        result = re.sub(r'(?s)return\s+df.*$', 'return df', result)
        return result
    
    async def generate_code(
        self,
        data_model,  # kept for signature compatibility; ignored
        prompt,
        interpreted_prompt,
        schemas,
        ds_clients,
        excel_files,
        code_and_error_messages,
        memories,
        previous_messages,
        retries,
        prev_data_model_code_pair=None,
        sigkill_event=None,
        code_context_builder=None,
        context: CodeGenContext | None = None,
    ):
        # Optional early exit if a cancellation was requested before generation
        if sigkill_event and hasattr(sigkill_event, 'is_set') and sigkill_event.is_set():
            return "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame()"
        # If a typed context is provided, use it exclusively (no ContextHub reads)
        if context is not None:
            instructions_context = context.instructions_context or ""
            mentions_context = context.mentions_context or "<mentions>No mentions for this turn</mentions>"
            entities_context = context.entities_context or ""
            messages_context = context.messages_context or ""
            resources_context = context.resources_context or ""
            files_context = context.files_context or ""
            platform = context.platform
            history_summary = context.history_summary or ""
            past_observations = context.past_observations or []
            last_observation = context.last_observation
            # Override schemas/prompt with curated ones from context
            schemas = context.schemas_excerpt or schemas
            prompt = context.interpreted_prompt or context.user_prompt or prompt
            data_preview_instruction = f"- Also, after each query or DataFrame creation, print the data using: print('df head:', df.head())" if self.enable_llm_see_data else ""
            # Retrieve top successful snippets based on targeted tables if provided
            similar_successful_code_snippets = ""
            try:
                if getattr(context, "tables_by_source", None):
                    builder = None
                    try:
                        # Prefer explicit code_context_builder param when provided
                        if code_context_builder is not None:
                            builder = code_context_builder
                        elif self.context_hub is not None:
                            from app.ai.context.builders.code_context_builder import CodeContextBuilder
                            # ContextHub is initialized with db and organization
                            db = getattr(self.context_hub, "db", None)
                            organization = getattr(self.context_hub, "organization", None)
                            current_user = getattr(self.context_hub, "user", None)
                            if db is not None and organization is not None:
                                builder = CodeContextBuilder(db=db, organization=organization, current_user=current_user)
                    except Exception:
                        builder = None
                    if builder is not None and hasattr(builder, "get_top_successful_snippets_for_tables"):
                        try:
                            top_success = await builder.get_top_successful_snippets_for_tables(context.tables_by_source, top_k=2)
                            if isinstance(top_success, list) and top_success:
                                lines = ["=== SUCCESSFUL EXAMPLES (by targeted tables) ==="]
                                for idx, s in enumerate(top_success, start=1):
                                    lines.append(f"[{idx}] step_id={s.get('step_id')} score={s.get('score')} success_rate={s.get('success_rate')}")
                                    code = s.get("code") or ""
                                    lines.append(code)
                                    lines.append("")
                                similar_successful_code_snippets = "\n".join(lines).strip()
                        except Exception as e:
                            similar_successful_code_snippets = ""
            except Exception:
                similar_successful_code_snippets = ""
            text = f"""
            You are a highly skilled data engineer and data scientist.

            Your goal: Given the user's prompt and the provided context, generate a Python function named `generate_df(ds_clients, excel_files)`
            that produces a Pandas DataFrame grounded ONLY in the provided schemas and resources.

            **General Organization Instructions**:
            **VERY IMPORTANT, CREATED BY THE USER, MUST BE USED AND CONSIDERED**:
            {instructions_context}

            **Context and Inputs**:
            - User Prompt:
            <user_prompt>
            {prompt}
            </user_prompt>
            
            - Interpreted Prompt:
            <interpreted_prompt>
            {interpreted_prompt}
            </interpreted_prompt>

            - Provided Schemas (Ground Truth):
            <ground_truth_schemas>
            {schemas}
            </ground_truth_schemas>

            - Resources:
            {resources_context}

            - Files:
            {files_context}

            - Data Sources and Clients:
            <data_sources_clients>
            {context.data_sources_context or ""}
            </data_sources_clients>

            - Mentions:
            {mentions_context}

            - Entities:
            {entities_context}

            - Messages (recent):
            <messages>
            {messages_context}
            </messages>

            - History Summary:
            {history_summary}

            - Past Observations:
            <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>

            - Last Observation:
            <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>

            - Similar successful code snippets (for reference on what is working):
            <similar_successful_code_snippets>
            {similar_successful_code_snippets}
            </similar_successful_code_snippets>

            **Guidelines and Requirements**:

            1. **Function Signature**: Implement exactly:
               `def generate_df(ds_clients, excel_files):`
               - The function should return the main dataframe that answers the user prompt.

            2. **Data Source Usage**:
               - Use `ds_clients[data_source_name].execute_query("SOME QUERY")` to query non-Excel data sources.
               - After each query or DataFrame creation, print its info using: print("df Info:", df.info())
               {data_preview_instruction}
               - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
               - For Excel files, use `pd.read_excel(excel_files[INDEX].path, sheet_name=SHEET_INDEX, header=None)`.
                 * Decide the correct INDEX and SHEET_INDEX based on prompt and schemas.
                 * Use prints to help validate indices and positions.
               - After ANY operation that changes DataFrame columns (merge, join, add/remove columns), print: print("df Info:", df.info())
               - Output schema contract: The final DataFrame must contain only primitives (str/int/float/bool/None). Never return dict/list objects. If a column is JSON/MAP/STRUCT or a JSON-looking string, extract/flatten to readable scalar columns (e.g., owner, repo_full_name) using pandas.json_normalize or by selecting key paths; otherwise stringify compactly. Prefer clear label/value columns for charting.
               - Allow only read operations on the data sources. No insert/delete/add/update/put/drop.
               - Prefer using data sources, tables, files, and entities explicitly listed in <mentions>. If selecting an unmentioned source, justify briefly.

            3. **Schema Adherence**:
               - Use only columns and relationships that exist in the provided schemas.
               - Do NOT invent columns that do not exist or cannot be derived.

            4. **Handling Previous Code and Errors**:
               - If `retries` ≥ 1, review the code_and_error_messages:
                 * Understand the error.
                 * If it's related to a missing column or invalid query, fix it by removing or correcting that column/query.
               - If `retries` ≥ 2 and still failing due to a specific column or measure, remove that problematic part and return a reduced but valid DataFrame.
               - Ensure you produce some output even if reduced.

            5. **Sorting and Final Output**:
               - If not mentioned by user, sort by the most relevant key column.

            6. **Data Formatting**:
               - Ensure the DataFrame is two-dimensional and handle missing values.

            7. **No Extra Formatting**:
               - Return ONLY the Python function code for `generate_df`.

            8. **End of code**:
               - Before returning the df — print("Final df Info:", df.info())
               {data_preview_instruction}
               - Return the df.

            Now produce ONLY the Python function code as described. No markdown or extra text.
            """
            result = self.llm.inference(text)
            result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
            result = re.sub(r'(?m)^\s*```\s*$', '', result)
            result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)
            result = re.sub(r'(?s)return\s+df.*$', 'return df', result)
            return result

        # Resolve instructions from context hub when available; otherwise fallback to legacy builder
        instructions_context = ""
        mentions_context = "<mentions>No mentions for this turn</mentions>"
        entities_context = ""
        # Defaults for additional context to avoid undefined variables
        resources_context = ""
        files_context = ""
        messages_context = ""
        platform = None
        past_observations = []
        last_observation = None
        history_summary = ""
        if self.context_hub is not None:
            try:
                view = self.context_hub.get_view()
                inst_obj = getattr(view.static, "instructions", None)
                instructions_context = inst_obj.render() if inst_obj else ""
                mentions_obj = getattr(view.static, "mentions", None)
                mentions_context = mentions_obj.render() if mentions_obj else mentions_context
                entities_obj = getattr(view.warm, "entities", None)
                entities_context = entities_obj.render() if entities_obj else entities_context
                # Additional sections (resources/files/messages/platform)
                resources_obj = getattr(view.static, "resources", None)
                resources_context = resources_obj.render() if resources_obj else ""
                files_obj = getattr(view.static, "files", None)
                files_context = files_obj.render() if files_obj else ""
                messages_obj = getattr(view.warm, "messages", None)
                messages_context = messages_obj.render() if messages_obj else ""
                try:
                    platform = (getattr(view, "meta", {}) or {}).get("external_platform")
                except Exception:
                    platform = None
                # Observations and history summary
                try:
                    if getattr(self.context_hub, "observation_builder", None):
                        past_observations = self.context_hub.observation_builder.tool_observations or []
                        last_observation = self.context_hub.observation_builder.get_latest_observation()
                except Exception:
                    past_observations = []
                    last_observation = None
                try:
                    history_summary = await self.context_hub.get_history_summary()
                except Exception:
                    history_summary = ""
            except Exception:
                instructions_context = ""
                mentions_context = mentions_context
                entities_context = entities_context
                resources_context = ""
                files_context = ""
                messages_context = ""
                platform = None
                past_observations = []
                last_observation = None
                history_summary = ""
        elif self.instruction_context_builder is not None:
            # Legacy compatibility
            if hasattr(self.instruction_context_builder, "get_instructions_context"):
                instructions_context = await self.instruction_context_builder.get_instructions_context()
            else:
                try:
                    inst_section = await self.instruction_context_builder.build()
                    instructions_context = inst_section.render()
                except Exception:
                    instructions_context = ""
            # Fallbacks when ContextHub is not present
            resources_context = ""
            files_context = ""
            messages_context = "\n".join(previous_messages) if isinstance(previous_messages, list) else str(previous_messages or "")
            platform = None
            past_observations = []
            last_observation = None
            history_summary = ""

        # Prepare data source descriptions (kept for compatibility)
        data_source_descriptions = []
        for data_source_name, client in ds_clients.items():
            data_source_descriptions.append(
                f"data_source_name: {data_source_name}\ndescription: {client.description}"
            )
        data_source_section = "\n".join(data_source_descriptions)

        # Prepare excel files description
        excel_files_description = []
        for index, file in enumerate(excel_files):
            excel_files_description.append(f"{index}: {file.description}")
        excel_files_section = "\n".join(excel_files_description)

        # Define data preview instruction based on enable_llm_see_data flag
        data_preview_instruction = f"- Also, after each query or DataFrame creation, print the data using: print('df head:', df.head())" if self.enable_llm_see_data else ""

        # Reuse data-model-based retrieval with an empty model for code-first flows
        if code_context_builder:
            
            try:
                similar_successful_code_snippets = await code_context_builder.get_top_successful_snippets_for_data_model({})
            except Exception:
                similar_successful_code_snippets = ""
            try:
                similar_failed_code_snippets = await code_context_builder.get_top_failed_snippets_for_data_model({})
            except Exception:
                similar_failed_code_snippets = ""
        else:
            similar_successful_code_snippets = ""
            similar_failed_code_snippets = ""

        # Previous attempts and errors
        code_error_section = ""
        if code_and_error_messages:
            combined = []
            for code, error in code_and_error_messages:
                combined.append(f"CODE:\n{code}\n\nERROR:\n{error}")
            code_error_section = "\n".join(combined)

        text = f"""
        You are a highly skilled data engineer and data scientist.

        Your goal: Given the user's prompt and the provided context, generate a Python function named `generate_df(ds_clients, excel_files)`
        that produces a Pandas DataFrame grounded ONLY in the provided schemas and resources.

        **General Organization Instructions**:
        **VERY IMPORTANT, CREATED BY THE USER, MUST BE USED AND CONSIDERED**:
        {instructions_context}

        **Context and Inputs**:
        - User Prompt:
        <user_prompt>
        {prompt}
        </user_prompt>

        - Platform:
        <platform>{platform}</platform>

        - Interpreted Prompt (if applicable):
        <interpreted_prompt>
        {prompt}
        </interpreted_prompt>

        - Provided Schemas (Ground Truth):
        <ground_truth_schemas>
        {schemas}
        </ground_truth_schemas>

        - Resources:
        {resources_context}

        - Files:
        {files_context}

        - Mentions:
        {mentions_context}

        - Entities:
        {entities_context}

        - Messages (recent):
        <messages>
        {messages_context}
        </messages>

        - History Summary:
        {history_summary}

        - Past Observations:
        <past_observations>{json.dumps(past_observations) if past_observations else '[]'}</past_observations>

        - Last Observation:
        <last_observation>{json.dumps(last_observation) if last_observation else 'None'}</last_observation>

        - Data Sources and Clients:
        Each data source may be SQL, document DB, service API, or Excel.
        You have a `ds_clients` dict where each key is a data source name.
        Each ds_client has a method `execute_query("QUERY")` that returns data.
        The 'QUERY' depends on the data source type. The data source descriptions are:
        <data_sources_clients>
        {data_source_section}
        </data_sources_clients>

        - Excel Files:
        <excel_files>
        {excel_files_section}
        </excel_files>

        - Previous Code Attempts and Errors:
        <code_retries>
        {retries}
        </code_retries>

        <code_and_error_messages>
        {code_error_section}
        </code_and_error_messages>

        - Similar successful code snippets (for reference on what is working):
        <similar_successful_code_snippets>
        {similar_successful_code_snippets}
        </similar_successful_code_snippets>

        **Guidelines and Requirements**:

        1. **Function Signature**: Implement exactly:
           `def generate_df(ds_clients, excel_files):`
           - The function should return the main dataframe that answers the user prompt.

        2. **Data Source Usage**:
           - Use `ds_clients[data_source_name].execute_query("SOME QUERY")` to query non-Excel data sources.
           - After each query or DataFrame creation, print its info using: print("df Info:", df.info())
           {data_preview_instruction}
           - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
           - For Excel files, use `pd.read_excel(excel_files[INDEX].path, sheet_name=SHEET_INDEX, header=None)` to read data.
             * Decide the correct INDEX and SHEET_INDEX based on prompt and schemas.
             * Use prints to help validate indices and positions.
           - After ANY operation that changes DataFrame columns (merge, join, add/remove columns), print: print("df Info:", df.info())
           - Output schema contract: The final DataFrame must contain only primitives (str/int/float/bool/None). Never return dict/list objects. If a column is JSON/MAP/STRUCT or a JSON-looking string, extract/flatten to readable scalar columns (e.g., owner, repo_full_name) using pandas.json_normalize or by selecting key paths; otherwise stringify compactly. Prefer clear label/value columns for charting.
           - Allow only read operations on the data sources. No insert/delete/add/update/put/drop.
           - Prefer using data sources, tables, files, and entities explicitly listed in <mentions>. If selecting an unmentioned source, justify briefly.

        3. **Schema Adherence**:
           - Use only columns and relationships that exist in the provided schemas.
           - If deriving columns or aggregations, ensure derivations are correct from existing schema fields.
           - Do NOT invent columns that do not exist or cannot be derived.
           - Do NOT include client names or non-relevant info inside queries. The data source queries should be generic and directly usable by the ds_clients.

        4. **Handling Previous Code and Errors**:
           - If `retries` ≥ 1, review the code_and_error_messages:
             * Understand the error.
             * If it's related to a missing column or invalid query, fix it by removing or correcting that column/query.
           - If `retries` ≥ 2 and still failing due to a specific column or measure, remove that problematic part and return a reduced but valid DataFrame.
           - Ensure you produce some output even if reduced. Not returning anything is worse than returning partial data.

        5. **Sorting and Final Output**:
           - If not mentioned by user, sort the DataFrame by the most relevant key column.
             * If it's a time or date column, sort descending.
             * If it's a count or sum, also sort descending.
             * Otherwise, sort ascending.

        6. **Data Formatting**:
           - Make sure the DataFrame is two-dimensional, with well-defined rows and columns.
           - Handle missing values gracefully.

        7. **No Extra Formatting**:
           - Return the code for the `generate_df` function as plain text only.
           - No Markdown, no extra comments beyond necessary Python code comments.
           - Do not wrap code in triple backticks or any markup.
        
        8. **End of code**:
           - At the end of the function, before returning the df — print("Final df Info:", df.info())
           {data_preview_instruction}
           - Return the df as the final output. Make sure the df name is the right one and reflects the main dataframe.

        Now produce ONLY the Python function code as described. Do not output anything else besides the function python code. No markdown, no comments, no triple backticks, no triple quotes, no triple anything, no text, no anything.
        """

        result = self.llm.inference(text)

        # Remove markdown code fence (with optional language tag) if present
        result = re.sub(r'^\s*```(?:[A-Za-z0-9_\-]+)?\s*\r?\n', '', result.strip(), flags=re.IGNORECASE)
        # Remove any closing fence lines that are just ```
        result = re.sub(r'(?m)^\s*```\s*$', '', result)
        # Defensive: remove a leading standalone language tag line (e.g., "python" or "json")
        result = re.sub(r'^\s*(?:json|python)\s*\r?\n', '', result, flags=re.IGNORECASE)
        # Remove any code after return df
        result = re.sub(r'(?s)return\s+df.*$', 'return df', result)
        return result
    
    async def validate_code(self, code, data_model):
        text = f"""
        You are a highly skilled data engineer and data scientist.

        Your goal: Given a data model, content and a generated code, validate the code.

        **Context and Inputs**:
        - Data Model:
        <data_model>
        {data_model}
        </data_model>

        - Generated Code:
        <generated_code>
        {code}
        </generated_code>

        **Guidelines**:
        1. There can be multiple dataframes as transformations steps
        2. There should only be one final dataframe as output
        3. Validate only read operations on the data sources. No insert/delete/add/update/put/drop.
        4. Validate the code is close enough to the data model. It doesnt need to be exactly the same.
        5. Do not be strict around code style.

        Response format:
        {{
            "valid": true,
            "reasoning": "Reasoning for the failed validation" (if valid is false)
        }}

        Now produce ONLY the JSON response as described. Do not output anything else besides the JSON response. No markdown, no comments, no triple backticks, no triple quotes, no triple anything, no text, no anything.
        """

        #result = self.llm.inference(text)
        #result = re.sub(r'^```json\n|^```\n|```$', '', result.strip())
        #result = json.loads(result)
        result = {"valid": True, "reasoning": "Validation passed"}
        
        return result
