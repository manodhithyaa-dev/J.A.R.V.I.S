MODEL_REGISTRY = {

    # =====================================================
    # 1. LOCAL MODELS — OLLAMA
    # =====================================================

    "local": {
        "provider": "ollama",
        "models": [
            {"id": "gemma4:12b", "name": "Gemma 4 12B", "size": "8 GB"},
            {"id": "gemma4:e4b", "name": "Gemma 4 E4B", "size": "6.6 GB"},
            {"id": "qwen3-embedding:4b", "name": "Qwen3 Embedding 4B", "size": "2.5 GB", "type": "embedding"},
            {"id": "sqlcoder:7b", "name": "SQLCoder 7B", "size": "4.1 GB"},
            {"id": "reader-lm:1.5b", "name": "Reader LM 1.5B", "size": "934 MB"},
            {"id": "glm4:9b", "name": "GLM 4 9B", "size": "5.5 GB"},
            {"id": "qwen3-vl:8b", "name": "Qwen3 VL 8B", "size": "6.1 GB", "type": "vision"},
            {"id": "tev1:4b", "name": "TEV1 4B", "size": "4.6 GB"},
            {"id": "legraphista/Orpheus:latest", "name": "Orpheus", "size": "2.4 GB", "type": "audio"},
            {"id": "duckdb-nsql:7b", "name": "DuckDB NSQL 7B", "size": "3.8 GB"},
            {"id": "medllama2:7b", "name": "MedLlama2 7B", "size": "3.8 GB"},
            {"id": "hermes3:8b", "name": "Hermes 3 8B", "size": "4.7 GB"},
            {"id": "cogito:8b", "name": "Cogito 8B", "size": "4.9 GB"},
            {"id": "llama3.2:3b", "name": "Llama 3.2 3B", "size": "2 GB"},
            {"id": "translategemma:4b", "name": "TranslateGemma 4B", "size": "3.3 GB"},
            {"id": "falcon3:7b", "name": "Falcon 3 7B", "size": "4.6 GB"},
            {"id": "qwen3.8:27b", "name": "Qwen 3.8 27B", "size": "17 GB"},
            {"id": "mistral:latest", "name": "Mistral", "size": "4.4 GB"},
            {"id": "qwen3-embedding:8b", "name": "Qwen3 Embedding 8B", "size": "4.7 GB", "type": "embedding"},
            {"id": "qwen3:8b", "name": "Qwen3 8B", "size": "5.2 GB"},
            {"id": "qwen3-coder:latest", "name": "Qwen3 Coder", "size": "18 GB"},
            {"id": "qwen2.5vl:7b", "name": "Qwen2.5 VL 7B", "size": "6 GB", "type": "vision"},
            {"id": "deepseek-r1:7b", "name": "DeepSeek R1 7B", "size": "4.7 GB"},
            {"id": "qwen2.5-coder:7b", "name": "Qwen2.5 Coder 7B", "size": "4.7 GB"},
            {"id": "qwen3:4b", "name": "Qwen3 4B", "size": "2.5 GB"},
            {"id": "gemma3:4b", "name": "Gemma 3 4B", "size": "3.3 GB"},
        ]
    },

    # =====================================================
    # 2. OPENROUTER — FREE MODELS
    # =====================================================

    "openrouter_free": {
        "provider": "openrouter",
        "models": [
            {
                "id": "openrouter/free",
                "name": "OpenRouter Auto Free",
                "description": "Automatically selects an available free model."
            },
            {
                "id": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "name": "Nemotron 3 Ultra",
                "description": "Advanced reasoning and agent workflows."
            },
            {
                "id": "nvidia/nemotron-3.5-super-vl:free",
                "name": "Nemotron 3.5 Super VL",
                "description": "Multimodal reasoning and vision."
            },
            {
                "id": "nvidia/nemotron-3.5-lightning:free",
                "name": "Nemotron 3.5 Lightning",
                "description": "Efficient agentic workloads."
            },
            {
                "id": "poolside/laguna-s-2.1:free",
                "name": "Laguna S 2.1",
                "description": "Coding and software engineering."
            },
            {
                "id": "dots-studio/dots-3-note-preview:free",
                "name": "Dots3 Note Preview",
                "description": "Reasoning, coding and multimodal tasks."
            },
            {
                "id": "thinkingmachines/inkling:free",
                "name": "Inkling",
                "description": "Reasoning, coding and multimodal chat."
            },
            {
                "id": "thinkingmachines/inkling-small:free",
                "name": "Inkling Small",
                "description": "Smaller multimodal general-purpose model."
            },
            {
                "id": "cohere/north-mini-code:free",
                "name": "North Mini Code",
                "description": "Coding and tool use."
            },
            {
                "id": "recursive/flint-v1.0:free",
                "name": "Flint",
                "description": "Coding agent and debugging."
            },
            {
                "id": "xingchen-agi/xing4.0-29b-a4b:free",
                "name": "Xing4.0 29B A4B",
                "description": "Reasoning and agent workflows."
            },
            {
                "id": "poolside/laguna-xs-2.1:free",
                "name": "Laguna XS 2.1",
                "description": "Lightweight coding agent."
            },
            {
                "id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
                "name": "Nemotron 3 Nano Omni",
                "description": "Multimodal text, image, video and audio."
            },
            {
                "id": "liquid/lfm-2.5-2.6b:free",
                "name": "Liquid LFM2.5 2.6B",
                "description": "Lightweight reasoning and extraction."
            },
            {
                "id": "nvidia/nemotron-3.5-content-safety:free",
                "name": "Nemotron Content Safety",
                "description": "Safety classification."
            }
        ]
    },

    # =====================================================
    # 3. GOOGLE GEMINI — PAID API OPTIONS
    # =====================================================

    "gemini": {
        "provider": "gemini",
        "models": [
            {"id": "gemini/gemini-4-argon", "name": "Gemini 4 Argon", "description": "Flagship reasoning."},
            {"id": "gemini/gemini-3.8-flash", "name": "Gemini 3.8 Flash", "description": "Advanced general-purpose and agentic tasks."},
            {"id": "gemini/gemini-3.7-flash", "name": "Gemini 3.7 Flash", "description": "Coding and agent workflows."},
            {"id": "gemini/gemini-3.6-flash", "name": "Gemini 3.6 Flash", "description": "Fast multimodal general-purpose model."},
            {"id": "gemini/gemini-3.5-flash", "name": "Gemini 3.5 Flash", "description": "General-purpose tasks."},
            {"id": "gemini/gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite", "description": "Cost-efficient tasks."},
            {"id": "gemini/gemini-3.1-flash-lite", "name": "Gemini 3.1 Flash Lite", "description": "Lightweight high-volume tasks."},
            {"id": "gemini/gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro", "description": "Complex reasoning and coding."},
            {"id": "gemini/gemini-3-flash-preview", "name": "Gemini 3 Flash", "description": "Frontier-class preview model."},
            {"id": "gemini/gemini-2.5-flash", "name": "Gemini 2.5 Flash", "description": "Legacy general-purpose model."},
            {"id": "gemini/gemini-2.5-pro", "name": "Gemini 2.5 Pro", "description": "Legacy advanced reasoning."}
        ]
    },

    # =====================================================
    # 4. OPENAI — PAID API OPTIONS
    # =====================================================

    "openai": {
        "provider": "openai",
        "models": [
            {"id": "openai/gpt-6-astra", "name": "GPT-6 Astra", "description": "Flagship intelligence."},
            {"id": "openai/gpt-6.1-sol", "name": "GPT-6.1 Sol", "description": "Advanced general-purpose intelligence."},
            {"id": "openai/gpt-6.1-terra", "name": "GPT-6.1 Terra", "description": "Efficient reasoning and visual tasks."},
            {"id": "openai/gpt-6-luna", "name": "GPT-6 Luna", "description": "Efficient high-volume tasks."},
            {"id": "openai/gpt-6-sol", "name": "GPT-6 Sol", "description": "Coding and agentic workflows."},
            {"id": "openai/gpt-5.6-sol", "name": "GPT-5.6 Sol", "description": "Advanced professional tasks."},
            {"id": "openai/gpt-5.6-terra", "name": "GPT-5.6 Terra", "description": "Balanced intelligence and cost."},
            {"id": "openai/gpt-5.6-luna", "name": "GPT-5.6 Luna", "description": "Cost-sensitive workloads."},
            {"id": "openai/gpt-5.5", "name": "GPT-5.5", "description": "Coding and professional work."},
            {"id": "openai/gpt-5.5-pro", "name": "GPT-5.5 Pro", "description": "Higher-precision responses."},
            {"id": "openai/gpt-5.4", "name": "GPT-5.4", "description": "General-purpose professional model."},
            {"id": "openai/gpt-5.4-pro", "name": "GPT-5.4 Pro", "description": "Advanced responses."},
            {"id": "openai/gpt-5.4-mini", "name": "GPT-5.4 Mini", "description": "Fast coding and computer-use tasks."},
            {"id": "openai/gpt-5.2", "name": "GPT-5.2", "description": "Reasoning and professional tasks."},
            {"id": "openai/gpt-5.2-pro", "name": "GPT-5.2 Pro", "description": "Higher-precision reasoning."}
        ]
    }
}