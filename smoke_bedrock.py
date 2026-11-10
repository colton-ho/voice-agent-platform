"""Smoke test: Azure AI Foundry LLM connectivity via the FC-3 adapter (one-shot).

Rewrote the Bedrock probe (mapping §5 F10): ``BEDROCK_MODEL_ID`` is deleted —
the target deployment comes from the frozen §11 contract vars (LLM_DEPLOYMENT
anchor, D-4 zero default), auth is Managed Identity (no keys). Run where Azure
credentials are available (e.g. ``az login`` or the VM system identity).
"""

import asyncio

from dotenv import load_dotenv

from providers.llm.azure_openai import make_managed_llm


async def main() -> None:
    load_dotenv()
    llm = make_managed_llm()

    reply = await llm.complete(
        messages=[
            {
                "role": "system",
                "content": "You are a friendly voice assistant. Keep replies to one sentence.",
            },
            {"role": "user", "content": "Say hi in Chinese in one short sentence."},
        ],
        max_tokens=128,
    )
    print(f"\n=== Foundry ({llm._deployment}) reply ===\n{reply}\n")


if __name__ == "__main__":
    asyncio.run(main())
