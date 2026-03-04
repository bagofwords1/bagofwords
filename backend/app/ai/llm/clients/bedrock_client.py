from typing import Optional

from app.ai.llm.clients.openai_client import OpenAi


class BedrockClient(OpenAi):
    """
    AWS Bedrock client using the OpenAI-compatible endpoint.
    Supports two auth modes:
    - api_key: Uses a Bedrock API key as Bearer token
    - iam: Generates a Bearer token from the AWS credential chain (IRSA, env vars, instance role, etc.)
    """

    def __init__(self, region: str, auth_mode: str = "iam", api_key: Optional[str] = None):
        base_url = f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"

        if auth_mode == "api_key":
            if not api_key:
                raise ValueError("Bedrock api_key auth mode requires an api_key")
            token = api_key
        else:
            # IAM auth: generate a Bearer token from the AWS credential chain
            token = self._generate_iam_token(region)

        super().__init__(api_key=token, base_url=base_url)
        self._region = region
        self._auth_mode = auth_mode

    @staticmethod
    def _generate_iam_token(region: str) -> str:
        """Generate a Bearer token for Bedrock using the AWS credential chain."""
        try:
            from aws_bedrock_token_generator import BedrockTokenGenerator

            generator = BedrockTokenGenerator(region=region)
            return generator.get_token()
        except ImportError:
            raise ImportError(
                "aws-bedrock-token-generator is required for IAM auth mode. "
                "Install it with: pip install aws-bedrock-token-generator"
            )
