from app.project_manager import ProjectManager
import os
from app.models.file import File
from app.models.llm_model import LLMModel
from app.ai.llm import LLM
import pandas as pd
import json
from pypdf import PdfReader
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTChar, LTAnno, LTTextLine, LTFigure


class DocAgent:

    def __init__(self, pdf_file: File, model: LLMModel):
        self.pdf_file = pdf_file
        self.llm = LLM(model)

    
    def get_content(self):

        file_path = self.pdf_file.path

        html_content = '<html><body>'

        for page_layout in extract_pages(file_path):
            html_content += '<div class="page">\n'

            for element in page_layout:
                if isinstance(element, LTTextContainer):
                    text = ''.join([text.get_text() for text in element])
                    html_content += f'<p>{text}</p>\n'

                html_content += '</div>\n'

        html_content += '</body></html>'

        return html_content


    def get_tags_from_text(self, html_content, previous_tags):

        prompt = f"""

        pdf_name:
        {self.pdf_file}

        html_content:
        {html_content}

        previous_tags:
        {previous_tags}

        You need to review the html content and provide a list of tags (key, value) that can be used to describe the content.
        For nested content, add the prefix of the parent tag to the nested tag.
        dont repeat tags that are already in the previous_tags
        
        For example:
        contract_number: 123456
        contract_date: 2024-01-01
        contract_amount: 1000
        contract_currency: USD
        contract_currency_symbol: $
        contract_currency_code: USD
        contract_currency_symbol: $
        contract_currency_code: USD
        contract_currency_symbol: $

        Response should be in the following format:

        [
            {{"tag": "contract_number", "value": "123456"}},
            {{"tag": "contract_date", "value": "2024-01-01"}},
            {{"tag": "contract_amount", "value": "1000"}},
            {{"tag": "contract_currency", "value": "USD"}},
            {{"tag": "contract_currency_symbol", "value": "$"}},
        ]

        Do not include any other text, comments, or explanations. Do not use markdown formatting. Respond with only the json schema, no other text.

        """

        tags = self.llm.inference(prompt)

        tags = json.loads(tags)

        return tags





    