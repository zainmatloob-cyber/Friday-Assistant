`# FRIDAY

A desktop assistant that actually does things.

```
you ▸ make a folder called hello in my E drive
  ⚙ create_folder(path='E:\hello')
    → Folder ready: E:\hello
FRIDAY ▸ Done — created E:\hello
```

## Setup (Windows)

**1. Install Python 3.10+** from python.org — tick *"Add Python to PATH"* during install.

**2. Install the dependency**

```powershell
cd path	oriday
pip install -r requirements.txt
```

**3. Get a Groq API key**

Sign up at https://console.groq.com — no credit card. Create a key under **API Keys**,
then set it permanently:

```powershell
setx GROQ_API_KEY "Your key"
```

If Groq reports `model_not_found`, choose a model ID available to your account in
the Groq console and set it as well:

```powershell
setx FRIDAY_MODEL "model-id-from-the-console"
```

Close and reopen PowerShell after `setx` — it only affects new terminals. The
default model is defined in `agent.py`.

**4. Run it**

```powershell
python friday.py
```

## Things to try

```
what's my system info
what's in my downloads folder
find every file with "resume" in the name on my E drive
make a folder E:\projects\test and put a hello.txt inside it saying hi
open chrome
what's using the most memory right now
```

That last one has no dedicated tool — FRIDAY routes it through PowerShell on its own.
That's the escape hatch doing its job.

## How it's wired

```
friday.py   the loop: your input → model → tool calls → results → model → reply
tools.py    the hands: real Python functions that touch the filesystem and shell
```

FRIDAY talks to Groq through the OpenAI-compatible endpoint, so `friday.py` uses the
`openai` package with `base_url` pointed at Groq. Switching providers later — Cerebras,
OpenRouter, a local Ollama server — is two constants at the top of the file. `tools.py`
never changes.

The model never touches your machine directly. It can only ask for one of the
functions in `tools.py`, with arguments matching that function's schema. Everything
it's capable of is in that one file — which means everything it's capable of is
something you wrote and can read.

## Adding a new power

Say you want volume control as a first-class tool rather than raw PowerShell.
Three additions, all in `tools.py`:

```python
def set_volume(level: int) -> str:                    # 1. the function
    ...
```
```python
{"name": "set_volume", "description": "...",          # 2. the schema
 "input_schema": {"type": "object",
                  "properties": {"level": {"type": "integer"}},
                  "required": ["level"]}}
```
```python
DISPATCH = {..., "set_volume": set_volume}            # 3. the wiring
```

Restart. FRIDAY now knows it can do that.

## Safety rails

`delete_path`, `move_path`, `write_text_file`, and `run_powershell` prompt for a
`y/N` before they run. This is not paranoia — it's because an LLM that
misreads "clear out the temp folder" while pointed at the wrong path is a real
failure mode, and the prompt costs you one keystroke.

Edit `NEEDS_CONFIRMATION` at the top of `tools.py` to loosen or tighten this.
When you get bored of confirming file writes, drop `write_text_file` from the set
first — keep `delete_path` and `run_powershell` in it.

Don't run this as Administrator until you trust it. You mostly won't need to.

## Rate limits, not bills

Groq's free tier costs nothing and asks for no card. What it does impose is rate limits,
applied **per organization and per model** — extra API keys do not raise them. You hit
whichever ceiling comes first: requests per minute, requests per day, or **tokens per
minute**.

Tokens per minute is the one that will bite you. Every FRIDAY command sends the system
prompt *plus all ten tool schemas* as input, twice (once to pick the tool, once to report
back). That's a few thousand tokens per command before you've said anything interesting.

Three defences are already in the code:

- `KEEP_MESSAGES = 14` trims old turns instead of re-sending the whole conversation
- typing `reset` clears memory between unrelated tasks
- 429s are caught and retried with backoff rather than crashing

Check your actual numbers on the **Limits** page in the Groq console — the published
figures move, and per-model limits differ a lot. If you're getting throttled, try a
smaller model first by setting `FRIDAY_MODEL` to an available model ID.

## Roadmap

- [x] **v1** — text in, tools out
- [ ] **v2** — voice: `speech_recognition` for input, `pyttsx3` for the reply
- [ ] **v3** — wake word ("hey friday") with `pvporcupine`, always listening
- [ ] **v4** — screen awareness: screenshot → vision model → "what's this error on my screen"
- [ ] **v5** — background daemon + global hotkey instead of a terminal window
