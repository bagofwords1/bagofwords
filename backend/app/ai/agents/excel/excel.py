from app.project_manager import ProjectManager
import os
from app.models.file import File
from app.ai.llm import LLM
import pandas as pd
import json
from app.models.llm_model import LLMModel

"""

given this excel file, please review and provide the following. for each field/metric you identify, respond with:
{ sheet: "sheetname": data: {"field/metric": { type: "", cell_address: "single value for the metric location, for example: A2" orientation: "vertical/horizontal" range: ""} }

"""

class ExcelAgent:

    def __init__(self, excel_file: File, model: LLMModel):
        self.excel_file = excel_file
        self.llm = LLM(model)

    

    def get_schema(self, index):

        file_path = self.excel_file.path

        xl = pd.ExcelFile(file_path)
        
        # Get the sheet name using the index
        sheet_name = xl.sheet_names[index]
        df = pd.read_excel(file_path, sheet_name=sheet_name, header=None)
        
        df_subset = df.iloc[0:200, 0:200].to_dict(orient='split')  # 'split' orientation keeps index, columns, and data

        # Now excel_dict contains the raw data as nested lists for each sheet

        prompt = f"""

        excel_name:
        {self.excel_file}

        sheet name:
        {sheet_name}

        sheet index:
        {index}

        excel dict:
        {df_subset} 

        Given this excel sample, please review and provide the following. 
        You need to generate a json schema for the excel file. You need to idenfiy all columns, metrics, and fields -- and for each
        of these you need to find the exact cell address, range, orientation, and type.
        It is very important to provide the exact cell address, range, orientation, and type. Validate it with the data in the file - sometimes it is not obvious where to find the data.   
        The schema should be in the following format:


        {{ sheet_name: "sheetname", sheet_index: [number of sheet], sheet_file_path: {file_path}, fields: {{ "field_name": "NAME!", "data": {{ type: "int/dec/string/date/boolean -- based on content values", cell_address: "single value for the metric location, for example: A2" orientation: "vertical/horizontal" range: ""}} }}

        respond with only the json schema, no other text. no comments, no explanations.
        and no markdown formatting.
        """

        schema = self.llm.inference(prompt)

        schema = json.loads(schema)

        return schema





    