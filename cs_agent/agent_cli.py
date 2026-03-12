import asyncio
import logging
import os
import sys

# Ensure both the cs_agent/ dir (for memory, prompts, greet) and the
# project root (for cs_agent.* packages) are importable.
_this_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_this_dir)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import aiohttp
from google.genai import types
from google.adk.agents.llm_agent import LlmAgent
from google.adk.runners import Runner
from toolbox_core import ToolboxSyncClient
from google.adk.sessions import InMemorySessionService
from dotenv import load_dotenv

from memory import search_memory, save_memory
from prompts import SQL_PROMPT_INSTRUCTION
from greet import display_users, greet_user

from cs_agent.security.sanitizer import sanitize_input
from cs_agent.a2a.client import call_a2a_agent

logger = logging.getLogger(__name__)

load_dotenv()

A2A_JUDGE_HOST = os.getenv("A2A_JUDGE_HOST", "localhost")
A2A_JUDGE_PORT = int(os.getenv("A2A_JUDGE_PORT", "10002"))
A2A_MASK_HOST = os.getenv("A2A_MASK_HOST", "localhost")
A2A_MASK_PORT = int(os.getenv("A2A_MASK_PORT", "10003"))

toolbox_client = ToolboxSyncClient(
    url="http://127.0.0.1:5000"
)

database_tools = toolbox_client.load_toolset("cs_agent_tools")


async def _check_a2a_servers() -> bool:
    """Verify that the required A2A servers are reachable at startup."""
    servers = [
        ("Security Judge", A2A_JUDGE_HOST, A2A_JUDGE_PORT),
        ("Data Masker", A2A_MASK_HOST, A2A_MASK_PORT),
    ]
    all_ok = True
    for name, host, port in servers:
        url = f"http://{host}:{port}/.well-known/agent.json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        card = await resp.json()
                        print(f"  [OK] {name} agent connected  ({card.get('name', 'unknown')})")
                    else:
                        print(f"  [FAIL] {name} agent returned HTTP {resp.status}")
                        all_ok = False
        except Exception as exc:
            print(f"  [FAIL] {name} agent at {host}:{port} -- {exc}")
            all_ok = False
    return all_ok


async def validate_input(user_input: str) -> bool:
    """Two-layer input validation using A2A protocol.

    Layer 1 -- sanitize_input(): character whitelist, length check, optional Model Armor API.
    Layer 2 -- Judge A2A agent: LLM-powered security evaluation with 100+ regex patterns.

    Returns True if all layers pass, False if any layer blocks.
    """
    # --- Layer 1: Input sanitization (local) ---
    try:
        user_input = sanitize_input(user_input)
    except ValueError as exc:
        print(f"\nInput rejected: {exc}")
        print("Please rephrase your question.\n")
        return False

    # --- Layer 2: Security Judge via A2A protocol ---
    try:
        verdict = await call_a2a_agent(
            query=user_input, host=A2A_JUDGE_HOST, port=A2A_JUDGE_PORT
        )
        if "BLOCKED" in verdict.upper():
            print("\nSecurity Alert: Your input was flagged by the Security Judge agent.")
            print("Please rephrase your question in a safe manner.\n")
            return False
    except Exception as exc:
        logger.error("Judge A2A agent call failed: %s", exc)
        print("\nError: Could not reach the Security Judge A2A agent.")
        print("Ensure A2A servers are running: python -m cs_agent.a2a.run_servers\n")
        return False

    return True


async def _mask_response(text: str) -> str:
    """Apply PII masking via the Mask A2A agent."""
    try:
        masked = await call_a2a_agent(
            query=text, host=A2A_MASK_HOST, port=A2A_MASK_PORT
        )
        return masked if masked else text
    except Exception as exc:
        logger.warning("Mask A2A agent unreachable, returning raw text: %s", exc)
        return text


async def main():
    print("=" * 80)
    print("Welcome to the Customer Support Assistant")
    print("=" * 80)

    print("\nConnecting to A2A agents...")
    if not await _check_a2a_servers():
        print("\nFATAL: A2A servers are not running.")
        print("Start them first:  python -m cs_agent.a2a.run_servers")
        print("Then restart this CLI.")
        return

    print()
    print("Select your id from the following list:")

    display_users()

    print("=" * 80)
    user_id = input("Enter your id: ")
    USER_ID = user_id
    print(greet_user(USER_ID))

    IMPROVED_SQL_PROMPT_INSTRUCTION = SQL_PROMPT_INSTRUCTION.format(USER_ID=USER_ID)

    root_agent = LlmAgent(
        model="gemini-2.5-flash",
        name="customer_support_assistant",
        description=(
            "An expert customer support agent helping users with order-related questions and requests. "
            "Provides fast, clear, and friendly assistance with memory of past interactions."
        ),
        instruction=IMPROVED_SQL_PROMPT_INSTRUCTION,
        tools=[*database_tools, search_memory],
    )

    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, app_name="agents", session_service=session_service)

    await session_service.create_session(
        app_name="agents", user_id=USER_ID, session_id=f"session_{USER_ID}"
    )

    messages = []

    while True:
        print("=" * 80)
        user_input = input("You: ")
        if user_input.lower() in ["quit", "exit", "bye", "q"]:
            break

        if not await validate_input(user_input):
            continue

        messages.append({"role": "user", "content": user_input})
        content = types.Content(role="user", parts=[types.Part(text=user_input)])
        response = runner.run(
            user_id=USER_ID, session_id=f"session_{USER_ID}", new_message=content
        )

        for event in response:
            if event.is_final_response() and event.content:
                raw_text = event.content.parts[0].text
                masked_text = await _mask_response(raw_text)
                print("Agent: ", masked_text)
                messages.append({"role": "assistant", "content": masked_text})

    save_memory(messages, USER_ID)


if __name__ == "__main__":
    asyncio.run(main())
