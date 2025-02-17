# Language Models

Bag of words allows users to integrate to a variety of language models. 

## Overview

A user can create a new LLM provider (that holds the credentials to the LLM provider) and an LLM model (that holds the model name and other configurations). 

The supported LLM providers are:
- OpenAI
- Anthropic

The supported LLM models are:
- OpenAI: GPT-4o, GPT-4o Mini
- Anthropic: Claude 3 variants

**Add LLM Provider:** To add an LLM provider, you need to add the provider type to the `LLM_PROVIDER_DETAILS` list in the `llm_provider.py` file.

**Add LLM Model:** To add an LLM model, you need to add the model type to the `LLM_MODEL_DETAILS` list in the `llm_model.py` file.

**Note2:** BOW Small is the default model for Bag of words SaaS deployments, and it is an abstraction of the OpenAI-4o-mini integration.



```mermaid
graph TD
    A[LLMProvider] --> B[Organization]
    A --> C[LLMModel]
    C --> B
    D[LLM Client] --> C
    D --> A
```

## Features
- Secure credential management
- Provider-specific configuration
- Model preset management 
- Organization-level customization
- Streaming and non-streaming inference

## Language Model Architecture

### Core Components

#### LLM Provider (`llm_provider.py`)
- Base database model for LLM service providers (OpenAI, Anthropic, Google, BOW)
- Handles encrypted credentials and provider-specific configs
- Links to organization and models

#### LLM Model (`llm_model.py`) 
- Database model for specific language models
- Predefined models per provider:
  - OpenAI: GPT-4o, GPT-4o Mini
  - Anthropic: Claude 3 variants
  - Google: Gemini models
  - BOW: BOW Small
- Links to provider and organization

#### LLM Schemas (`llm_schema.py`)
- Pydantic models for validation and serialization
- Provider schemas with credentials and configs
- Model schemas for CRUD operations

#### LLM Client (`llm.py`)
- Main interface for LLM interactions
- Dynamic provider client selection
- Supports sync/async inference

### Relationships
```mermaid
mermaid
graph TD
A[LLMProvider] --> B[Organization]
A --> C[LLMModel]
C --> B
D[LLM Client] --> C
D --> A
```
