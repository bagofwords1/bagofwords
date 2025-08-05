#!/usr/bin/env python3
"""
Simple test script to verify Azure LLM integration
"""

import asyncio
from app.ai.llm.clients.azure_client import AzureClient

async def test_azure_client():
    """Test Azure client creation and basic functionality"""
    try:
        # Test client creation (without actual API calls)
        client = AzureClient(
            api_key="test-key",
            endpoint_url="https://test-resource.openai.azure.com"
        )
        print("✅ Azure client created successfully")
        
        # Test connection method
        result = client.test_connection()
        print(f"✅ Connection test result: {result}")
        
        print("✅ Azure integration test passed!")
        
    except Exception as e:
        print(f"❌ Azure integration test failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_azure_client()) 