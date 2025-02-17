from partialjson.json_parser import JSONParser
from app.ai.llm import LLM
from app.models.llm_model import LLMModel
import re

class Coder:
    def __init__(self, model: LLMModel) -> None:
        self.llm = LLM(model)

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

        # The final prompt text
        # Improvements made:
        # 1. Clear, step-by-step instructions.
        # 2. Strong emphasis on using the exact schema columns and correct data source queries.
        # 3. Detailed instructions on how to handle retries and errors.
        # 4. Clear instructions on sorting, data types, and final DataFrame structure.
        # 5. Emphasis on reading previous code if provided and adapting it.
        # 6. Explicit instructions to not leak client names into queries and to handle them through ds_clients keys only.
        # 7. Stress the importance of providing output even if some columns fail after multiple retries.
        # 8. Organized sections for clarity.

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
           def generate_df(ds_clients, excel_files):

        2. **Data Source Usage**:
           - Use `ds_clients[data_source_name].execute_query("SOME QUERY")` to query non-Excel data sources.
           - After each query or DataFrame creation, print the columns using: print("df Columns:", df.columns)
           - For SQL data sources, "SOME QUERY" should be SQL code that matches the schema column names exactly.
           - For Excel files, use `pd.read_excel(excel_files[INDEX].path, sheet_name=SHEET_INDEX, header=None)` to read data.
             * Decide the correct INDEX and SHEET_INDEX based on prompt and data model.
             * Convert all columns to strings.
           - After ANY operation that changes DataFrame columns (merge, join, add/remove columns), print: print("df Columns:", df.columns)

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
           - Ensure all final columns in the DataFrame are strings (convert if needed).

        6. **Data Formatting**:
           - Make sure the DataFrame is two-dimensional, with well-defined rows and columns.
           - Handle missing values gracefully.
           - All columns should end up as strings, even numeric fields.

        7. **No Extra Formatting**:
           - Return the code for the `generate_df` function as plain text only.
           - No Markdown, no extra comments beyond necessary Python code comments.
           - Do not wrap code in triple backticks or any markup.
        
        8. **End of code**:
           - At the end of the function, before returning the df — print the df.columns last time

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
            


# # UNUSED FOR NOW
#     async def update_widget_code(self, data_model, code):
# 
#         You need to generate a Python function that creates the dataframe based on the preivous data model, but updated with the new data model changes.
#         You have the previous code as a baseline. At the end, you will need to provide changes to the original code
# 
#         Please follow these steps:
# 
#         1. Generate a Python function named `generate_df` that receives two arguments: a `db_clients` list and an `excel_files` list.
#         2. Inside the function, you can use `db_clients[INDEX].execute_query("YOUR SQL CODE")` to query the database and return a dataframe.
#         - Ensure to use the correct index for the database client.
#         - Use the EXACT column names from the ground truth schema and adhere to the relationships mentioned in the data model. Ignore the generated column names in the data model if they do not exist in the schema.
#         3. For reading spreadsheets, use the `excel_files` list.
#         - Based on the user prompt, decide which Excel file and sheet index to use.
#         - To read an Excel file, use `pd.read_excel(excel_files[RELEVANT_INDEX].path, sheet_name=RELEVANT_SHEET_INDEX, header=None)`. Always use `header=None`.
#         - Ensure to use the correct index for the Excel file and the appropriate sheet index.
#         - Ensure to use the correct data types based on the Excel schema when reading data.
#         4. Generate code to create a dataframe based on the data model.
#         5. Ensure the dataframe is two-dimensional, with columns and rows.
#         6. Every column in the data model should be present in the dataframe. Regardless of user prompt.
#         7. If retries is 1 or more, make sure to read the error and previous code - carefully
#         8. if retries is 2 or more, and the error is related to a specific column/measure -- remove it from code and deliver the code without it
#         9. If retries is 2 or more, do anything you can to have something outputted!! reaching to max retries (3) and not delivering is horrible
# 
# 
#         Guidelines:
#         - Make sure to use the sources correctly: for Excel, use the file name, and for databases, use the database client.
#         - Use database clients efficiently.
#         - Available libraries: pandas, numpy.
#         - When reading from the database, use the EXACT column names from the schema.
#         - When reading from Excel, use ONLY indices and cell addresses, not names.
#         - Ensure the function is generic and can be applied to similar data models.
#         - Ensure the function can handle dataframes with empty or missing values.
#         - Ensure the function can handle dataframes with missing or additional columns.
#         - All columns should be strings (even if they represent numbers, booleans, etc.).
#         - If there are errors in the code and specified in context, make sure you understand the error, and think step-by-step about the solution and implement the code
# 
#         Respond with the list of replaces to the original code only. Format should with the following guidelines:
#         - Use the exact identation as required per line
#         - For removing a line, use "\\n"
#         - When removing a line, make sure to update lines before/after as needed in case of commas, strings, trailing spaces, etc
#         - For adding a line, use the exact identation as required per line
#         - if adding multiple lines -- make sure to add each line as a separate change
#         - dont use line numbers in the replace string
#         - If the error repeats, make sure to understand the error and fix it
#         - If the error repeats more than once, provide changes for more code lines (+-2 lines per change)
#         [
#             {{
#                 "replace": "  df['column_X'] = df['column_X'] * 2\na = 5 \n b=5\nx=55 \n \n", (SPECIFY THE EXACT IDENTATION REQUIRED)
#                 "line_start": 1  (POSITION IS THE LINE NUMBER, START FROM 1)
#                 "line_end": 5 (IF ADDING MULTIPLE LINES- SPECIFY THE LAST LINE NUMBER, OTHERWISE SAME AS POSITION)
#             }},
#             {{
#                 "replace": "    db_clients[0].execute_query("SELECT * FROM table_X")",
#                 "line_start": 23,
#                 "line_end": 23
#             }}
#             {{
#                 "replace": "\\n" # for removing line, use new line
#                 "line_start": 14,
#                 "line_end": 14
#             }},
#             {{
#                 "replace": "df['column_Z'] = df['column_Z'] / 10",
#                 "line_start": 23,
#                 "line_end": 23
#             }}
#         ]
# 
#         Respond with the diff only, no markdown, no prefix no ``` or anything. just the diff as raw text.
# 
#         ### Important Guidelines for JSON Output:
#         - If you are generating multi-line code, ensure all newlines are escaped using `\\n`.
#         - Make sure all quotes inside strings are escaped using a backslash (`\\`).
#         - Ensure that the JSON output is properly formatted, with correct commas and brackets.
#         - Avoid any extra text, and make sure the output is valid JSON.
#         - Keep the identation as required per line
#         - No markdown, no prefix no ``` or anything. just the diff as raw text.
#         - Never start the answer wiwth ```json.
#         - always return the list ONLY. no prefix or anything.
#         """