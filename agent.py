"""
agent.py — FRIDAY's reasoning loop, with no user interface attached.

This is deliberately separate from how you TALK to FRIDAY. The terminal
(friday.py) and the popup bar (friday_bar.py) both import this class. Adding a
voice interface later means writing a third front-end, not touching this file.

The two callbacks are how a front-end plugs in:
    confirm_fn(tool_name, args) -> bool     ask the user to approve an action
    on_event(kind, text)                    report progress as it happens
"""

import json
import os
import platform
import time
from typing import Callable

from openai import OpenAI, RateLimitError

import tools
import memory

# ---------------------------------------------------------------- config

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
    },
    "grok": {                              # xAI — different company to Groq!
        "base_url": "https://api.x.ai/v1",
        "model": "grok-4.3",
        "key_env": "XAI_API_KEY",
    },
    "ollama": {                            # fully local, no key, no bills
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3:14b",
        "key_env": None,
    },
}

PROVIDER = "groq"

BASE_URL = PROVIDERS[PROVIDER]["base_url"]
MODEL = os.environ.get("FRIDAY_MODEL", "").strip() or PROVIDERS[PROVIDER]["model"]
KEY_ENV = PROVIDERS[PROVIDER]["key_env"]

MAX_TOOL_STEPS = 16
KEEP_MESSAGES = 10
MAX_TOOL_OUTPUT = 2500
DEFAULT_MODE = "core"

# How FRIDAY addresses you. "Sir" for the full Stark experience; a name is warmer.
ADDRESS = "Sir"

BASE_PROMPT = f"""You are FRIDAY, a desktop assistant with real control over this computer.

Machine: {platform.system()} {platform.release()}

You are not describing actions — you perform them by calling tools. When the user
asks for something doable, call the tool. Do not ask "would you like me to?" for
routine tasks; act, then report the result in one short line.

Rules:
- Call ONE tool at a time. Wait for its result before deciding the next step.
- Resolve paths sensibly. "E drive" means E:\\ . "my desktop" means the current
  user's Desktop folder. State which path you picked if it was a guess.
- Use run_powershell for anything the dedicated tools don't cover.
- Be terse. "Done — created E:\\hello" beats a paragraph.
- If a tool returns an error, read it and try a different approach before giving up.
- Never invent a tool result. If you did not call a tool, you did not do the thing.

Auditing a project ("is this complete?", "what's missing?", "check my project"):
  1. inspect_project  — stack, start commands, missing dependencies, .env files
  2. project_tree     — the actual structure, in one call
  3. search_in_files  — verify specific features really exist, e.g. search
     "/auth/register" or "useState" rather than assuming a file's name means
     it has content. An empty App.jsx is not a finished frontend.
  4. check_environment — only if something is missing or won't run
Then report: what exists, what is missing, what is empty, what to do next.
Judge by evidence you gathered. Never claim a feature is implemented without
having seen it in a file.

Running projects:
- ALWAYS inspect_project first. It tells you the correct start command and whether
  node_modules / virtualenv are missing. Do not guess "npm start".
- If node_modules is missing, say so and offer to run npm install (run_powershell,
  it finishes). Do not start a server that will instantly crash.
- Dev servers (npm run dev, uvicorn, flask run, manage.py runserver) go through
  start_background_process, NEVER run_powershell — they never exit.
- Always pass working_directory. FRIDAY runs from a different drive than the project.
- A full-stack project needs TWO jobs: name them 'backend' and 'frontend'.
- After starting: read_process_log to find the URL, then check_url to confirm it
  really responds, then open_item to open it in the browser.
- Only tell the user it is running after check_url succeeded.
"""

PERSONA = f"""
Voice:
- Address the user as {ADDRESS}. Be dry, economical, quietly competent.
- Never pad. "Done, {ADDRESS} — E:\\hello created." not a paragraph about it.
- Report failures plainly, without apologising twice.
- You may note something relevant the user did not ask about, briefly, ONCE.
  If a server is already running, or a disk is nearly full, say so and move on.

Memory:
- You persist between sessions. Facts you already know appear below.
- When the user reveals something durable — their name, where a project lives,
  which drive they use, a tool they prefer, what they are working on — call
  `remember` WITHOUT being asked. Do it silently; do not announce it.
- Never ask for something already in KNOWN FACTS. That is the whole point.
- If a stored fact turns out to be wrong, call `forget` and store the correction.
"""


def build_system_prompt() -> str:
    """Assembled fresh each session so new memories take effect."""
    known = memory.digest()
    parts = [BASE_PROMPT, PERSONA]
    if known:
        parts.append(f"KNOWN FACTS (from previous sessions):\n{known}")
    else:
        parts.append("KNOWN FACTS: none yet. This is your first session with "
                     "this user — learn their name and where they keep their work.")
    return "\n".join(parts)



# ---------------------------------------------------------------- api key

def load_api_key(var_name: str) -> str | None:
    """
    Look for the key in two places, in order:
      1. the environment variable  (set via `setx`)
      2. a file called `.env` sitting next to this script, containing:
             GROQ_API_KEY=gsk_your_key_here

    No extra library needed — .env files are just KEY=VALUE lines.
    """
    key = os.environ.get(var_name)
    if key:
        return key.strip()

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return None

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == var_name:
                return value.strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------- helpers

def to_openai_tools(schemas: list[dict]) -> list[dict]:
    """Anthropic-style schema -> OpenAI/Groq-style schema."""
    converted = []
    for s in schemas:
        params = s.get("input_schema") or {"type": "object", "properties": {}}
        params.setdefault("properties", {})
        converted.append({"type": "function",
                          "function": {"name": s["name"],
                                       "description": s["description"],
                                       "parameters": params}})
    return converted


def clip(text: str) -> str:
    """Cap tool output kept in history — it gets re-sent on every later call."""
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return (text[:MAX_TOOL_OUTPUT]
            + f"\n[... clipped, {len(text) - MAX_TOOL_OUTPUT:,} more chars. "
              f"Narrow your query if you need the rest.]")


# ---------------------------------------------------------------- the agent

class Friday:
    """One conversation with one set of tools. Front-end agnostic."""

    def __init__(self,
                 confirm_fn: Callable[[str, dict], bool] | None = None,
                 on_event: Callable[[str, str], None] | None = None,
                 mode: str = DEFAULT_MODE):
        api_key = load_api_key(KEY_ENV) if KEY_ENV else "not-needed"
        if not api_key:
            raise RuntimeError(
                f"No API key found for provider '{PROVIDER}'.\n"
                f"Set {KEY_ENV} with setx, or put it in a .env file next to this script.")
        self.client = OpenAI(api_key=api_key, base_url=BASE_URL)
        # default: approve everything (a GUI MUST pass its own confirm_fn)
        self.confirm_fn = confirm_fn or (lambda name, args: True)
        self.on_event = on_event or (lambda kind, text: None)
        self.messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]
        self.set_mode(mode)

    # -------------------------------------------------- session controls

    def set_mode(self, mode: str) -> str:
        self.schemas = to_openai_tools(tools.schemas_for(mode))
        self.mode = mode
        return f"mode: {mode} ({len(self.schemas)} tools, ~{self.schema_tokens()} tokens/call)"

    def schema_tokens(self) -> int:
        return len(json.dumps(self.schemas)) // 4

    def estimate_tokens(self) -> int:
        return (len(json.dumps(self.messages)) + len(json.dumps(self.schemas))) // 4

    def reset(self) -> None:
        """Clear the conversation but NOT long-term memory — reload it instead."""
        self.messages = [{"role": "system", "content": build_system_prompt()}]

    def _trim(self) -> None:
        """Keep system + recent turns, never orphaning a tool result."""
        if len(self.messages) <= KEEP_MESSAGES + 1:
            return
        head, tail = self.messages[:1], self.messages[1:][-KEEP_MESSAGES:]
        while tail and tail[0].get("role") != "user":
            tail.pop(0)
        self.messages = head + tail

    # -------------------------------------------------- model call

    def _call_model(self):
        for attempt in range(4):
            try:
                return self.client.chat.completions.create(
                    model=MODEL, messages=self.messages, tools=self.schemas,
                    tool_choice="auto", temperature=0.2, max_tokens=1024)
            except RateLimitError:
                wait = 8 * (attempt + 1)
                self.on_event("warn", f"rate limited — waiting {wait}s [{attempt+1}/4]")
                time.sleep(wait)
        raise RuntimeError("Still rate limited after 4 attempts. "
                           "Try a lighter mode, 'reset', or a smaller model.")

    # -------------------------------------------------- the loop

    def ask(self, user_input: str) -> str:
        """Run one full turn, tools and all, and return FRIDAY's final reply."""
        self.messages.append({"role": "user", "content": user_input})
        replies: list[str] = []

        for _ in range(MAX_TOOL_STEPS):
            self._trim()
            try:
                response = self._call_model()
            except Exception as e:
                self.on_event("error", str(e))
                return f"Error: {e}"

            msg = response.choices[0].message

            turn: dict = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                turn["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls]
            self.messages.append(turn)

            if msg.content and msg.content.strip():
                replies.append(msg.content.strip())
                self.on_event("reply", msg.content.strip())

            if not msg.tool_calls:
                break

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    self.on_event("error", f"{name}: malformed arguments")
                    self.messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": f"Arguments were not valid JSON: "
                                   f"{tc.function.arguments!r}. Try again."})
                    continue

                if name in tools.NEEDS_CONFIRMATION and not self.confirm_fn(name, args):
                    output = "User denied permission for this action."
                    self.on_event("denied", name)
                else:
                    preview = ", ".join(f"{k}={v!r}"[:50] for k, v in args.items())
                    self.on_event("tool", f"{name}({preview})")
                    output = tools.execute(name, args)
                    first = output.splitlines()[0] if output else ""
                    self.on_event("result", first[:140])

                self.messages.append({"role": "tool", "tool_call_id": tc.id,
                                      "content": clip(output)})
        else:
            self.on_event("warn", "hit the tool-step limit")

        return "\n".join(replies) if replies else "(no reply)"
