"""
friday.py — terminal front-end.

Run:  python friday.py

Commands:
  /mode [core|dev|safe|all]   swap which tools are loaded
  tokens                      show current cost per API call
  reset                       clear conversation memory
  exit                        quit
"""

import sys

import agent
import memory
import tools

CYAN, YELLOW, GREY, RED, RESET = "\033[96m", "\033[93m", "\033[90m", "\033[91m", "\033[0m"


def confirm(tool_name: str, args: dict) -> bool:
    print(f"{YELLOW}  ⚠  FRIDAY wants to run {tool_name}{RESET}")
    for k, v in args.items():
        shown = str(v)
        print(f"{YELLOW}       {k}: {shown[:300]}{' …' if len(shown) > 300 else ''}{RESET}")
    try:
        return input(f"{YELLOW}     allow? [y/N] {RESET}").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def on_event(kind: str, text: str) -> None:
    if kind == "tool":
        print(f"{GREY}  ⚙ {text}{RESET}")
    elif kind == "result":
        print(f"{GREY}    → {text}{RESET}")
    elif kind == "reply":
        print(f"{CYAN}FRIDAY ▸{RESET} {text}")
    elif kind == "denied":
        print(f"{RED}  ✗ denied{RESET}")
    elif kind in ("warn", "error"):
        print(f"{RED}  {text}{RESET}")


def main() -> None:
    try:
        friday = agent.Friday(confirm_fn=confirm, on_event=on_event)
    except RuntimeError as e:
        sys.exit(str(e))

    print(f"{CYAN}FRIDAY online ({agent.PROVIDER} / {agent.MODEL}){RESET}")
    print(f"{GREY}{friday.set_mode(friday.mode)}  |  '/mode dev' to switch, "
          f"'reset' clears memory, 'exit' quits{RESET}\n")

    while True:
        try:
            text = input(f"{CYAN}you ▸ {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoing offline.")
            return

        if not text:
            continue
        low = text.lower()

        if low in {"exit", "quit", "bye"}:
            print("Going offline.")
            return
        if low == "reset":
            friday.reset()
            print(f"{GREY}  memory cleared{RESET}\n")
            continue
        if low in {"memory", "/memory"}:
            print(f"{GREY}  {memory.stats()}{RESET}")
            print(f"{GREY}{memory.recall()}{RESET}\n")
            continue
        if low == "forget all":
            print(f"{YELLOW}  This erases everything FRIDAY knows about you.{RESET}")
            if input(f"{YELLOW}  sure? [y/N] {RESET}").strip().lower() in {"y", "yes"}:
                print(f"{GREY}  {memory.wipe()}{RESET}")
                friday.reset()
            print()
            continue
        if low == "tokens":
            print(f"{GREY}  ~{friday.estimate_tokens():,} tokens per call "
                  f"(mode={friday.mode}, {len(friday.messages)} messages){RESET}\n")
            continue
        if low.startswith("/mode"):
            parts = text.split()
            options = ", ".join(list(tools.TOOL_GROUPS) + ["all"])
            if len(parts) < 2:
                print(f"{GREY}  modes: {options}  (currently: {friday.mode}){RESET}\n")
                continue
            try:
                print(f"{GREY}  {friday.set_mode(parts[1])}{RESET}\n")
            except KeyError:
                print(f"{RED}  unknown mode '{parts[1]}'. Options: {options}{RESET}\n")
            continue

        friday.ask(text)   # on_event already printed everything
        print()


if __name__ == "__main__":
    main()
