from app.ai.llm import LLM
from app.models.llm_model import LLMModel
from app.schemas.organization_settings_schema import OrganizationSettingsConfig
import tiktoken 
import json
from partialjson.json_parser import JSONParser
from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

class Judge:

    def __init__(self, model: LLMModel, organization_settings: OrganizationSettingsConfig, instruction_context_builder: InstructionContextBuilder) -> None:
        self.llm = LLM(model)
        self.organization_settings = organization_settings
        self.instruction_context_builder = instruction_context_builder

    async def score_instructions_and_context(self, prompt, schemas, memories, previous_messages) -> tuple[int, int]:
        """
        Score the relevance of instructions and context for the user's request.
        Returns (instructions_score, context_score) both 1-5 scale.
        """
        try:
            # Get organization instructions
            instructions_context = await self.instruction_context_builder.get_instructions_context()
            
            scoring_prompt = f"""
            You are an expert evaluator assessing the quality and relevance of instructions and context for a data analytics request.

            **USER'S REQUEST:**
            {prompt}

            **ORGANIZATION INSTRUCTIONS:**
            {instructions_context}

            **AVAILABLE SCHEMAS:**
            {schemas}

            **MEMORIES:**
            {memories if memories else "No memories available"}

            **PREVIOUS MESSAGES:**
            {previous_messages if previous_messages else "No previous conversation"}

            **SCORING TASK:**
            Evaluate two aspects on a 1-5 scale where:
            - 1 = Poor/Irrelevant
            - 2 = Below Average
            - 3 = Average/Adequate
            - 4 = Good/Relevant
            - 5 = Excellent/Highly Relevant

            1. **Instructions Effectiveness (1-5)**: How well do the organization instructions help guide this specific user request? Consider:
               - If no need for instructions, the question is clear and the current context is sufficient, return 5

            2. **Context Effectiveness (1-5)**: How relevant and sufficient is the available context (schemas, memories, previous messages) for fulfilling this request? Consider:
               - Do the available data schemas contain the information needed?
               - Is there enough information to complete the request successfully?
               - If no need for special additional context, the question is clear and the current context is sufficient, return 5

            **OUTPUT FORMAT:**
            Return ONLY a JSON object with no additional text:
            {{
                "instructions_score": <1-5 integer>,
                "context_score": <1-5 integer>,
                "reasoning": "Brief explanation of both scores"
            }}
            """

            response = self.llm.inference(scoring_prompt)
            
            try:
                scores = json.loads(response)
                instructions_score = max(1, min(5, int(scores.get("instructions_score", 3))))
                context_score = max(1, min(5, int(scores.get("context_score", 3))))
                return instructions_score, context_score
            except (json.JSONDecodeError, ValueError, TypeError):


                # Fallback to default scores if JSON parsing fails
                return 3, 3

        except Exception as e:
            print(f"Error in score_instructions_and_context: {e}")
            return 3, 3  # Default middle scores on error

    async def score_response_quality(self, original_prompt, widgets_and_steps, observation_data=None) -> int:
        """
        Score the overall quality of the agent's response against the original user intent.
        Returns response_score 1-5 scale.
        """
        try:
            # Build summary of what was created
            widgets_summary = []
            for widget, step in widgets_and_steps:
                if widget and step:
                    summary = f"""
                    Widget: {widget.title}
                    Type: {step.data_model.get('type', 'unknown') if step.data_model else 'unknown'}
                    Status: {step.status}
                    Rows: {len(step.data.get('rows', [])) if step.data else 0}
                    Columns: {len(step.data.get('columns', [])) if step.data else 0}
                    """
                    widgets_summary.append(summary)

            widgets_summary_text = "\n".join(widgets_summary) if widgets_summary else "No widgets created"

            # Include observation data if available
            observation_summary = ""
            if observation_data and observation_data.get("widgets"):
                observation_summary = f"""
                **FINAL RESULTS SUMMARY:**
                {len(observation_data['widgets'])} widgets were successfully created and executed.
                """

            scoring_prompt = f"""
            You are an expert evaluator assessing the quality of an AI agent's response to a user's data analytics request.

            **ORIGINAL USER REQUEST:**
            {original_prompt}

            **WHAT THE AGENT CREATED:**
            {widgets_summary_text}

            {observation_summary}

            **SCORING TASK:**
            Evaluate the overall response quality on a 1-5 scale where:
            - 1 = Poor: Failed to address the request, major errors, irrelevant output
            - 2 = Below Average: Partially addressed request, significant issues
            - 3 = Average: Adequately addressed request, minor issues
            - 4 = Good: Well addressed request, meets expectations
            - 5 = Excellent: Perfectly addressed request, exceeded expectations

            **EVALUATION CRITERIA:**
            1. **Completeness**: Did the agent fully address what the user asked for?
            2. **Accuracy**: Are the widgets/analysis appropriate for the request?
            3. **Data Quality**: Do the results contain meaningful, relevant data?
            4. **User Intent**: Does the output align with the user's apparent goals?
            5. **Execution Success**: Were the widgets successfully created and populated?

            **IMPORTANT:**
            - If the question was vague but the agent created a good response, return 4 or 5
            - If the question was clear but the agent created a bad response, return 1 or 2
            - If the question was clear and the agent created a good response, return 5


            **OUTPUT FORMAT:**
            Return ONLY a JSON object with no additional text:
            {{
                "response_score": <1-5 integer>,
                "reasoning": "Very brief explanation of the score"
            }}
            """

            response = self.llm.inference(scoring_prompt)
            
            try:
                score_data = json.loads(response)
                response_score = max(1, min(5, int(score_data.get("response_score", 3))))
                return response_score
            except (json.JSONDecodeError, ValueError, TypeError):
                # Fallback to default score if JSON parsing fails
                return 3

        except Exception as e:
            print(f"Error in score_response_quality: {e}")
            return 3  # Default middle score on error