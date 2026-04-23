"""
Arcade x Galileo demo: a raw-Python agent loop.

Three moving parts, in order of appearance below:
  1. Galileo  -- wraps the OpenAI client so every LLM call is auto-traced.
  2. Arcade   -- supplies the tools (MCP-backed under the hood) and executes them with managed auth.
  3. An LLM-driven loop -- plain `chat.completions.create(...)` + tool_calls, no agent framework.

Adopt this into your codebase by lifting the Galileo wrapper lines and the `run_arcade_tool`
function; the loop itself is generic OpenAI function-calling and will already look familiar.
"""

import json
import os

from dotenv import load_dotenv

# --- 1. Galileo --------------------------------------------------------------
# The OpenAI import below is Galileo's drop-in wrapper: identical API to `openai.OpenAI`,
# but every chat.completions call is auto-logged to the active Galileo trace.
from galileo import galileo_context, log
from galileo.openai import OpenAI

# --- 2. Arcade ---------------------------------------------------------------
from arcadepy import Arcade


load_dotenv()

USER_ID = os.environ["USER_ID"]
TOOLKIT = "math"  # no-auth toolkit -- runs end-to-end with only API keys, no browser OAuth.
MODEL = "gpt-4o-mini"
PROMPT = "What is 17 * 23, then take the square root of that result? Use tools."

galileo_context.init(
    project=os.environ["GALILEO_PROJECT"],
    log_stream=os.environ["GALILEO_LOG_STREAM"],
)

llm = OpenAI()          # Galileo-wrapped OpenAI client.
arcade = Arcade()       # Reads ARCADE_API_KEY from env.


# --- Tool discovery ----------------------------------------------------------
# Arcade can hand us tool schemas pre-formatted for OpenAI function-calling,
# so we skip the manual JSON-schema conversion. The shape is:
#   [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}, ...]
# Iterating the pager (rather than .items) handles toolkits with >1 page of tools.
tools = list(arcade.tools.formatted.list(format="openai", toolkit=TOOLKIT))


# --- Arcade execution, as a Galileo tool span --------------------------------
# @log(span_type="tool") attaches this function's input/output to the active
# Galileo trace as a tool span, tying execution results back to the LLM's tool_call.
@log(span_type="tool")
def run_arcade_tool(tool_name: str, tool_args: dict) -> str:
    result = arcade.tools.execute(tool_name=tool_name, input=tool_args, user_id=USER_ID)
    if result.status == "failed":
        return f"ERROR: {result.output.error if result.output else 'unknown'}"
    return json.dumps(result.output.value) if result.output else ""


# --- 3. Agent loop -----------------------------------------------------------
@log  # wraps the whole run as a single Galileo workflow span.
def main() -> None:
    messages: list = [{"role": "user", "content": PROMPT}]

    while True:
        resp = llm.chat.completions.create(model=MODEL, messages=messages, tools=tools)
        msg = resp.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            print(msg.content)
            return

        for tc in msg.tool_calls:
            output = run_arcade_tool(tc.function.name, json.loads(tc.function.arguments))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})


if __name__ == "__main__":
    try:
        main()
    finally:
        galileo_context.flush()  # ensure traces reach Galileo before exit.
