from typing import List, Tuple, AsyncGenerator, Optional

from .clients.openai_client import OpenAi
from .clients.google_client import Google
from .clients.anthropic_client import Anthropic
from .clients.bow_client import Bow
from app.models.llm_model import LLMModel
from app.ai.logging import LLMCallLogger

class LLM:
    def __init__(self, model: LLMModel, llm_call_logger: Optional[LLMCallLogger] = None):
        self.model = model
        self.model_id = model.model_id
        self.provider = model.provider.provider_type
        self.api_key = self.model.provider.decrypt_credentials()[0]
        self.llm_call_logger = llm_call_logger

        if self.provider == "openai":
            self.client = OpenAi(api_key=self.api_key)
        elif self.provider == "anthropic":
            self.client = Anthropic(api_key=self.api_key)
        elif self.provider == "google":
            self.client = Google()
        elif self.provider == "bow":
            self.client = Bow(api_key=self.api_key)
        else:
            raise ValueError(f"Provider {self.provider} not supported")
 
    def inference(self, prompt: str, execution_log_id: Optional[str] = None, report_id: Optional[str] = None, completion_id: Optional[str] = None) -> str:
        print(f"Model: {self.model_id}, prompt: {prompt}")
        
        # Log the LLM call if logger is available
        if self.llm_call_logger:
            # Use async context manager for logging - note this is a sync method so we'll need to handle this differently
            # For now, we'll add basic logging
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                llm_call_log = loop.run_until_complete(
                    self.llm_call_logger.start_llm_call(
                        provider=self.provider,
                        model_id=self.model_id,
                        call_type="inference",
                        prompt=prompt,
                        is_streaming=False,
                        execution_log_id=execution_log_id,
                        report_id=report_id,
                        completion_id=completion_id
                    )
                )
                
                try:
                    response = self.client.inference(model_id=self.model_id, prompt=prompt)
                    
                    # End the log with success
                    loop.run_until_complete(
                        self.llm_call_logger.end_llm_call(
                            llm_call_log,
                            status="completed",
                            response=response
                        )
                    )
                    
                except Exception as e:
                    # End the log with failure
                    loop.run_until_complete(
                        self.llm_call_logger.end_llm_call(
                            llm_call_log,
                            status="failed",
                            error_message=str(e)
                        )
                    )
                    raise
                    
            except Exception as log_error:
                print(f"Warning: LLM call logging failed: {log_error}")
                # Continue with the actual LLM call even if logging fails
                response = self.client.inference(model_id=self.model_id, prompt=prompt)
        else:
            response = self.client.inference(model_id=self.model_id, prompt=prompt)
        
        print(f"response: {response}")
        return response
    
    async def inference_stream(self, prompt: str, execution_log_id: Optional[str] = None, report_id: Optional[str] = None, completion_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        print(f"Model: {self.model_id}, prompt: {prompt}")
        
        # Log the streaming LLM call if logger is available
        if self.llm_call_logger:
            # Use the streaming call logger
            original_stream = self.client.inference_stream(model_id=self.model_id, prompt=prompt)
            async for chunk in self.llm_call_logger.log_streaming_call(
                provider=self.provider,
                model_id=self.model_id,
                prompt=prompt,
                stream_generator=original_stream,
                execution_log_id=execution_log_id,
                report_id=report_id,
                completion_id=completion_id
            ):
                yield chunk
        else:
            async for chunk in self.client.inference_stream(model_id=self.model_id, prompt=prompt):
                yield chunk
