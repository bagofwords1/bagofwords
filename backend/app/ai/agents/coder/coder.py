from partialjson.json_parser import JSONParser
from app.ai.llm import LLM
from app.models.llm_model import LLMModel
import re
import json

class Coder:
    def __init__(self, model: LLMModel, organization_settings: dict) -> None:
        self.llm = LLM(model)
        self.organization_settings = organization_settings
        self.enable_llm_see_data = organization_settings.get("allow_llm_see_data", {}).get("enabled", True)

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
        prev_data_model_code_pair
    ):
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

        text = f"""
        You are a highly skilled data engineer and data scientist.

        Your goal: Given a data model and context, generate a Python function named `generate_df(ds_clients, excel_files)`
        that produces a Pandas DataFrame according to the data model specifications.

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
           - After ANY operation that changes DataFrame columns (merge, join, add/remove columns), print: print("df Preview:", {data_preview_instruction})
           - Allow only read operations on the data sources. No insert/delete/add/update/put/drop.

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

        **Approach**:
        - Start from scratch or modify the existing code if `prev_data_model_code_pair` is provided.
        - Integrate data from `ds_clients` and `excel_files` as needed.
        - Carefully build queries.
        - Test logic in your mind to avoid errors.
        - If error hints are provided (from previous retries), address them directly.

        Now produce ONLY the Python function code as described. Do not output anything else besides the function python code. No markdown, no comments, no triple backticks, no triple quotes, no triple anything, no text, no anything.
        """

        result = self.llm.inference(text)
        # Remove markdown code block indicators if present
        result = re.sub(r'^```python\n|^```\n|```$', '', result.strip())
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

        result = self.llm.inference(text)
        result = re.sub(r'^```json\n|^```\n|```$', '', result.strip())
        result = json.loads(result)

        return result
