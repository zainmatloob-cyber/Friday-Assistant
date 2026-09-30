"""
tools.py — FRIDAY's hands.

Every function here is something FRIDAY can actually DO to your machine.
Each returns a plain string, which gets fed back to the model as the result.

To give FRIDAY a new power:
  1. Write a normal Python function below.
  2. Add its schema to TOOL_SCHEMAS.
  3. Add it to DISPATCH.
That's it. No other file needs to change.
"""

import os
import shutil
import subprocess
import platform
import datetime
import tempfile
import time
import signal
import json
import re
import urllib.request
import urllib.error
from pathlib import Path

import memory

# Tools in this set will ask you for confirmation before running.
NEEDS_CONFIRMATION = {"delete_path", "move_path", "run_powershell",
                      "write_text_file", "start_background_process"}

# Don't let a search crawl your entire drive forever.
MAX_SEARCH_RESULTS = 40


# ---------------------------------------------------------------- filesystem

def create_folder(path: str) -> str:
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return f"Folder ready: {p.resolve()}"


def list_directory(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"Not a directory: {p}"
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    if not entries:
        return f"{p} is empty."
    lines = []
    for e in entries[:200]:
        tag = "DIR " if e.is_dir() else "FILE"
        size = "" if e.is_dir() else f"  ({e.stat().st_size:,} bytes)"
        lines.append(f"  {tag}  {e.name}{size}")
    more = f"\n  ...and {len(entries) - 200} more" if len(entries) > 200 else ""
    return f"Contents of {p.resolve()}:\n" + "\n".join(lines) + more


def search_files(name_contains: str, root: str = "C:\\Users") -> str:
    root_p = Path(root).expanduser()
    if not root_p.exists():
        return f"Search root does not exist: {root_p}"
    needle = name_contains.lower()
    hits = []
    for dirpath, dirnames, filenames in os.walk(root_p, onerror=lambda e: None):
        # skip noisy system dirs
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and d.lower() not in {"node_modules", "appdata", "$recycle.bin",
                                             "windows", "program files", "program files (x86)"}]
        for f in filenames:
            if needle in f.lower():
                hits.append(str(Path(dirpath) / f))
                if len(hits) >= MAX_SEARCH_RESULTS:
                    return (f"Found {len(hits)} matches (stopped at limit):\n  "
                            + "\n  ".join(hits))
    if not hits:
        return f"No files matching '{name_contains}' under {root_p}"
    return f"Found {len(hits)} matches:\n  " + "\n  ".join(hits)


def read_text_file(path: str, max_chars: int = 4000) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"Not a file: {p}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Could not read {p}: {e}"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[truncated — file is {len(text):,} chars total]"
    return text or "[file is empty]"


def write_text_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    verb = "Overwrote" if existed else "Created"
    return f"{verb} {p.resolve()} ({len(content):,} chars)"


def move_path(source: str, destination: str) -> str:
    src, dst = Path(source).expanduser(), Path(destination).expanduser()
    if not src.exists():
        return f"Source does not exist: {src}"
    shutil.move(str(src), str(dst))
    return f"Moved {src} -> {dst}"


def delete_path(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Nothing to delete at {p}"
    if p.is_dir():
        shutil.rmtree(p)
        return f"Deleted folder {p.resolve()} and everything inside it."
    p.unlink()
    return f"Deleted file {p.resolve()}"


# ---------------------------------------------------------------- launching

def open_item(target: str) -> str:
    """Open a file, folder, URL, or installed app by name (notepad, chrome, spotify...)."""
    system = platform.system()
    try:
        if system == "Windows":
            path = Path(target).expanduser()
            if path.exists():
                os.startfile(str(path))  # type: ignore[attr-defined]
                return f"Opened {path.resolve()}"
            subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
            return f"Launched '{target}'"
        elif system == "Darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return f"Opened '{target}'"
    except Exception as e:
        return f"Could not open '{target}': {e}"


# ---------------------------------------------------------------- escape hatch

def _shell_args(command: str) -> list[str]:
    if platform.system() == "Windows":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    return ["bash", "-lc", command]


def run_powershell(command: str, working_directory: str = "") -> str:
    """
    The catch-all for commands that FINISH: git status, pip install, a build,
    a script that prints and exits. Waits up to 120s and returns the output.

    Do NOT use this for servers — see start_background_process.
    """
    cwd = None
    if working_directory:
        cwd_p = Path(working_directory).expanduser()
        if not cwd_p.is_dir():
            return f"working_directory does not exist: {cwd_p}"
        cwd = str(cwd_p)
    try:
        proc = subprocess.run(_shell_args(command), cwd=cwd,
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ("Command timed out after 120 seconds. If this is a server or "
                "watcher that never exits, use start_background_process instead.")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"exit code: {proc.returncode}"]
    if out:
        parts.append(f"stdout:\n{out[:3000]}")
    if err:
        parts.append(f"stderr:\n{err[:1500]}")
    return "\n".join(parts)


# ---------------------------------------------------------------- background jobs

# Long-running things (dev servers, watchers) live here instead of blocking.
_BACKGROUND: dict[str, dict] = {}


def start_background_process(command: str, working_directory: str = "",
                             name: str = "") -> str:
    """
    Launch something that is SUPPOSED to keep running — a dev server, a watcher.
    Returns immediately. Output is streamed to a log file you can read later.
    """
    cwd = None
    if working_directory:
        cwd_p = Path(working_directory).expanduser()
        if not cwd_p.is_dir():
            return f"working_directory does not exist: {cwd_p}"
        cwd = str(cwd_p)

    name = name or f"job{len(_BACKGROUND) + 1}"
    if name in _BACKGROUND and _BACKGROUND[name]["proc"].poll() is None:
        return f"A job named '{name}' is already running. Stop it first, or use another name."

    log_path = Path(tempfile.gettempdir()) / f"friday_{name}.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")

    kwargs = {}
    if platform.system() == "Windows":
        # own process group, so we can kill the whole tree later
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(_shell_args(command), cwd=cwd, stdout=log_file,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, **kwargs)

    _BACKGROUND[name] = {"proc": proc, "log": log_path,
                         "command": command, "cwd": cwd or os.getcwd()}

    time.sleep(3)  # give it a moment to crash, if it's going to
    if proc.poll() is not None:
        tail = _tail(log_path, 25)
        return (f"Job '{name}' exited immediately with code {proc.returncode}. "
                f"It did not stay running. Output:\n{tail}")

    return (f"Started '{name}' (PID {proc.pid}) in {cwd or os.getcwd()}\n"
            f"Command: {command}\n"
            f"Still running. Use read_process_log('{name}') to see its output "
            f"(the URL it's serving on usually appears there).")


def _tail(path: Path, lines: int) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[could not read log: {e}]"
    if not content.strip():
        return "[no output yet]"
    return "\n".join(content.splitlines()[-lines:])


def read_process_log(name: str, lines: int = 30) -> str:
    """Read recent output from a background job."""
    job = _BACKGROUND.get(name)
    if not job:
        return f"No job named '{name}'. Known jobs: {list(_BACKGROUND) or 'none'}"
    status = "running" if job["proc"].poll() is None else f"exited ({job['proc'].returncode})"
    return f"Job '{name}' [{status}]\nLast {lines} lines:\n{_tail(job['log'], lines)}"


def list_background_processes() -> str:
    """Show every background job FRIDAY has started this session."""
    if not _BACKGROUND:
        return "No background jobs running."
    rows = []
    for name, job in _BACKGROUND.items():
        code = job["proc"].poll()
        state = "running" if code is None else f"exited({code})"
        rows.append(f"  {name}  [{state}]  PID {job['proc'].pid}  {job['command'][:60]}")
    return "Background jobs:\n" + "\n".join(rows)


def stop_background_process(name: str) -> str:
    """Stop a background job and everything it spawned."""
    job = _BACKGROUND.get(name)
    if not job:
        return f"No job named '{name}'. Known jobs: {list(_BACKGROUND) or 'none'}"
    proc = job["proc"]
    if proc.poll() is not None:
        return f"Job '{name}' had already exited (code {proc.returncode})."
    try:
        if platform.system() == "Windows":
            # /T kills child processes too — node spawns several
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception as e:
        return f"Could not cleanly stop '{name}': {e}"
    return f"Stopped '{name}' (PID {proc.pid})."


# ---------------------------------------------------------------- inspection

# Folders that are never interesting and would swamp any listing.
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env",
             "dist", "build", ".next", ".vite", ".pytest_cache", ".idea",
             ".vscode", "coverage", ".mypy_cache", "site-packages", ".turbo"}

TEXT_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".css",
             ".scss", ".html", ".yml", ".yaml", ".toml", ".env", ".example",
             ".cfg", ".ini", ".sql", ".sh"}


def check_environment(programs: str = "") -> str:
    """
    Report which development tools are installed and their versions.
    Pass a comma-separated list, or leave blank for the usual suspects.
    """
    default = "node,npm,python,pip,git,docker,psql"
    wanted = [p.strip() for p in (programs or default).split(",") if p.strip()]

    # how to ask each program for its version
    flags = {"psql": "--version", "docker": "--version"}
    rows = []
    for prog in wanted:
        exe = shutil.which(prog)
        if not exe:
            rows.append(f"  {prog:<10} NOT INSTALLED (not on PATH)")
            continue
        flag = flags.get(prog, "--version")
        try:
            proc = subprocess.run([exe, flag], capture_output=True, text=True, timeout=15)
            version = (proc.stdout or proc.stderr or "").strip().splitlines()
            version = version[0] if version else "(no version output)"
        except Exception as e:
            version = f"(found, but version check failed: {e})"
        rows.append(f"  {prog:<10} {version}")
    return "Environment:\n" + "\n".join(rows)


def project_tree(path: str, max_depth: int = 3, max_entries: int = 300) -> str:
    """
    Show a project's folder structure in ONE call, skipping node_modules and
    other noise. This is how FRIDAY judges whether a project is complete.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        return f"Not a directory: {root}"

    lines, count, truncated = [], 0, False

    def walk(d: Path, prefix: str, depth: int):
        nonlocal count, truncated
        if depth > max_depth or truncated:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            return
        entries = [e for e in entries if not (e.is_dir() and e.name in SKIP_DIRS)]
        for i, e in enumerate(entries):
            if count >= max_entries:
                truncated = True
                return
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            if e.is_dir():
                lines.append(f"{prefix}{branch}{e.name}/")
                count += 1
                walk(e, prefix + ("    " if last else "│   "), depth + 1)
            else:
                size = e.stat().st_size
                lines.append(f"{prefix}{branch}{e.name}  ({size:,}b)")
                count += 1

    walk(root, "", 1)
    skipped = [d.name for d in root.iterdir() if d.is_dir() and d.name in SKIP_DIRS] \
        if root.is_dir() else []
    footer = f"\n[truncated at {max_entries} entries]" if truncated else ""
    if skipped:
        footer += f"\n[present but not listed: {', '.join(skipped)}]"
    return f"{root.resolve()}/\n" + "\n".join(lines) + footer


def inspect_project(path: str) -> str:
    """
    Audit a project: what stack is it, are dependencies installed, is there an
    .env, does it have a git repo, what are the start commands.
    Use this BEFORE trying to run anything.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        return f"Not a directory: {root}"

    report = [f"Project audit: {root.resolve()}", ""]

    # find sub-projects (a full-stack repo has several manifests)
    manifests = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if Path(dirpath).relative_to(root).parts.__len__() > 2:
            continue
        for f in filenames:
            if f in {"package.json", "requirements.txt", "pyproject.toml",
                     "docker-compose.yml", "Dockerfile"}:
                manifests.append(Path(dirpath) / f)

    if not manifests:
        report.append("No package.json / requirements.txt found. "
                      "This may not be a runnable project.")

    for m in manifests:
        rel = m.relative_to(root)
        folder = m.parent
        report.append(f"--- {rel} ---")

        if m.name == "package.json":
            try:
                data = json.loads(m.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                report.append(f"  UNREADABLE: {e}")
                continue
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            stack = []
            for lib, label in [("react", "React"), ("vite", "Vite"), ("next", "Next.js"),
                               ("express", "Express"), ("tailwindcss", "Tailwind"),
                               ("recharts", "Recharts"), ("framer-motion", "Framer Motion"),
                               ("axios", "Axios"), ("react-router-dom", "React Router")]:
                if lib in deps:
                    stack.append(f"{label} {deps[lib]}")
            report.append(f"  name: {data.get('name', '?')}")
            report.append(f"  detected: {', '.join(stack) if stack else 'plain node'}")
            scripts = data.get("scripts", {})
            report.append(f"  scripts: {', '.join(scripts) if scripts else 'NONE'}")
            for key in ("dev", "start"):
                if key in scripts:
                    report.append(f"    -> start with: npm run {key}   (cwd: {folder})")
                    break
            installed = (folder / "node_modules").is_dir()
            report.append(f"  node_modules: {'present' if installed else 'MISSING — run npm install first'}")

        elif m.name in {"requirements.txt", "pyproject.toml"}:
            try:
                text = m.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                report.append(f"  UNREADABLE: {e}")
                continue
            found = [lib for lib in ("fastapi", "uvicorn", "sqlalchemy", "pydantic",
                                     "alembic", "psycopg2", "django", "flask",
                                     "pytest", "python-jose", "passlib")
                     if lib in text.lower()]
            report.append(f"  detected: {', '.join(found) if found else 'unknown python project'}")
            if "fastapi" in found:
                entry = "app.main:app" if (folder / "app" / "main.py").exists() else "main:app"
                report.append(f"    -> start with: uvicorn {entry} --reload   (cwd: {folder})")
            venv = any((folder / v).is_dir() for v in (".venv", "venv", "env"))
            report.append(f"  virtualenv: {'present' if venv else 'none found in project folder'}")

        else:
            report.append(f"  (container config present)")
        report.append("")

    # env files
    envs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.startswith(".env"):
                envs.append(str(Path(dirpath).relative_to(root) / f))
    report.append(f"env files: {', '.join(envs) if envs else 'NONE — app may fail to start'}")

    # git
    if (root / ".git").is_dir():
        try:
            p = subprocess.run(["git", "status", "--short"], cwd=str(root),
                               capture_output=True, text=True, timeout=20)
            changes = len([l for l in p.stdout.splitlines() if l.strip()])
            report.append(f"git: repo present, {changes} uncommitted change(s)")
        except Exception:
            report.append("git: repo present")
    else:
        report.append("git: not a repository")

    return "\n".join(report)


def search_in_files(pattern: str, root: str, file_types: str = "",
                    max_hits: int = 40) -> str:
    """
    Search file CONTENTS (not names) for a string. This is how FRIDAY answers
    'is the /auth/register endpoint implemented?' without reading every file.
    """
    root_p = Path(root).expanduser()
    if not root_p.is_dir():
        return f"Not a directory: {root_p}"

    exts = {e.strip() if e.strip().startswith(".") else "." + e.strip()
            for e in file_types.split(",") if e.strip()} or None
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)

    hits = []
    for dirpath, dirnames, filenames in os.walk(root_p):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if exts and fp.suffix.lower() not in exts:
                continue
            if not exts and fp.suffix.lower() not in TEXT_EXTS:
                continue
            try:
                if fp.stat().st_size > 2_000_000:
                    continue
                for n, line in enumerate(fp.read_text(encoding="utf-8",
                                                      errors="replace").splitlines(), 1):
                    if rx.search(line):
                        rel = fp.relative_to(root_p)
                        hits.append(f"  {rel}:{n}: {line.strip()[:120]}")
                        if len(hits) >= max_hits:
                            return (f"Found {len(hits)} matches for '{pattern}' "
                                    f"(limit reached):\n" + "\n".join(hits))
            except Exception:
                continue
    if not hits:
        return f"No matches for '{pattern}' under {root_p}"
    return f"Found {len(hits)} matches for '{pattern}':\n" + "\n".join(hits)


def check_url(url: str, timeout: int = 5) -> str:
    """
    Check whether a server is actually responding. Use after starting a dev
    server to confirm it booted, before telling the user it's ready.
    """
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FRIDAY"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(400).decode("utf-8", errors="replace")
            return (f"{url} responded {resp.status} {resp.reason}\n"
                    f"First bytes: {body[:200]}")
    except urllib.error.HTTPError as e:
        return f"{url} responded {e.code} {e.reason} (server is up, returned an error)"
    except urllib.error.URLError as e:
        return f"{url} is NOT responding: {e.reason}. Server may still be starting, or failed to boot."
    except Exception as e:
        return f"{url} check failed: {type(e).__name__}: {e}"


# ---------------------------------------------------------------- memory

def remember(key: str, value: str, category: str = "general") -> str:
    """Store something durable about the user, their machine, or their projects."""
    return memory.remember(key, value, category)


def recall(query: str = "") -> str:
    """Look up stored facts."""
    return memory.recall(query)


def forget(key: str) -> str:
    """Delete a stored fact."""
    return memory.forget(key)


# ---------------------------------------------------------------- info

def system_info() -> str:
    drives = ""
    if platform.system() == "Windows":
        rows = []
        for letter in "CDEFGH":
            root = f"{letter}:\\"
            if os.path.exists(root):
                try:
                    usage = shutil.disk_usage(root)
                    rows.append(f"  {root}  {usage.free // 2**30} GB free "
                                f"of {usage.total // 2**30} GB")
                except OSError:
                    pass
        drives = "\nDrives:\n" + "\n".join(rows) if rows else ""
    return (f"OS: {platform.system()} {platform.release()}\n"
            f"Machine: {platform.node()}\n"
            f"Processor: {platform.processor()}\n"
            f"Time: {datetime.datetime.now():%A %d %B %Y, %I:%M %p}"
            + drives)


# ---------------------------------------------------------------- schemas

TOOL_SCHEMAS = [
    {
        "name": "create_folder",
        "description": "Create a folder (and any missing parent folders) at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": r"Full path, e.g. 'E:\hello' or 'C:\Users\me\Desktop\notes'"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List the files and folders inside a directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": "Recursively search for files whose name contains a substring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "root": {"type": "string",
                         "description": r"Where to start searching, e.g. 'E:\' or 'C:\Users'. Defaults to the user folder."},
            },
            "required": ["name_contains"],
        },
    },
    {
        "name": "read_text_file",
        "description": "Read the contents of a text file (code, .txt, .md, .json, .csv).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_text_file",
        "description": "Write text to a file, creating it or overwriting it entirely.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "move_path",
        "description": "Move or rename a file or folder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["source", "destination"],
        },
    },
    {
        "name": "delete_path",
        "description": "Permanently delete a file, or a folder and all its contents. Not the recycle bin — this is gone for good.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "open_item",
        "description": "Open a file, folder, website URL, or installed application. Examples: 'notepad', 'chrome', 'https://youtube.com', 'E:\\hello'.",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "run_powershell",
        "description": (
            "Run a shell command that FINISHES on its own and return its output. "
            "Good for: git status, npm install, pip install, builds, a script that "
            "prints and exits, checking processes, network info. "
            "Do NOT use for dev servers or anything that keeps running — "
            "it will time out after 120 seconds. Use start_background_process for those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "working_directory": {
                    "type": "string",
                    "description": r"Folder to run in, e.g. 'E:\myproject'. Important when the command depends on project files.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "start_background_process",
        "description": (
            "Start a long-running process that is meant to keep running: a dev server "
            "(npm run dev, npm start, python manage.py runserver, uvicorn, flask run), "
            "a file watcher, a database. Returns immediately instead of waiting. "
            "For a full-stack project, call this once for the backend and once for the "
            "frontend, with different names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "e.g. 'npm run dev'"},
                "working_directory": {"type": "string",
                                      "description": r"Folder to run in, e.g. 'E:\myproject\client'"},
                "name": {"type": "string",
                         "description": "Short label, e.g. 'frontend' or 'backend'. Used to read logs or stop it."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_process_log",
        "description": ("Read recent output from a background job. Use this after starting a "
                        "server to check it booted and to find the URL it's serving on."),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "lines": {"type": "integer"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_background_processes",
        "description": "List all background jobs started this session and whether they're still running.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_background_process",
        "description": "Stop a background job (and its child processes) by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "check_environment",
        "description": ("Check which development tools are installed on this machine and their "
                        "versions (node, npm, python, pip, git, docker, psql). Use this before "
                        "trying to run a project, to explain WHY something can't run."),
        "input_schema": {
            "type": "object",
            "properties": {"programs": {"type": "string",
                                        "description": "Comma-separated, e.g. 'node,npm,python'. Blank checks the common ones."}},
        },
    },
    {
        "name": "project_tree",
        "description": ("Show a project's whole folder structure in one call, skipping "
                        "node_modules and similar noise. Use this to judge whether a project "
                        "is complete, instead of calling list_directory many times."),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_depth": {"type": "integer", "description": "Default 3. Use 4-5 for deep projects."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "inspect_project",
        "description": ("Audit a project folder: detect the stack (React/Vite/FastAPI/etc), find "
                        "the correct start commands, check whether node_modules and virtualenvs "
                        "are installed, list .env files, report git status. ALWAYS call this "
                        "before attempting to run an unfamiliar project."),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_in_files",
        "description": ("Search the CONTENTS of files for a string or regex. Use this to verify "
                        "a feature exists, e.g. searching for '/auth/register' to check an "
                        "endpoint is implemented, or 'useState' to see if components are real."),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "root": {"type": "string"},
                "file_types": {"type": "string",
                               "description": "Optional comma-separated extensions, e.g. '.py,.jsx'"},
            },
            "required": ["pattern", "root"],
        },
    },
    {
        "name": "check_url",
        "description": ("Check whether a URL responds. Use after starting a dev server to confirm "
                        "it actually booted before telling the user it's ready."),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "e.g. 'http://localhost:5173'"}},
            "required": ["url"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Store a durable fact so you still know it in future sessions. Call this "
            "WITHOUT being asked whenever the user reveals something worth keeping: "
            "their name, where their projects live, which drive they use, tools they "
            "prefer, what they are currently working on. Use a short stable key "
            "('user_name', 'careerlens_path') so repeat facts overwrite instead of piling up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "short snake_case identifier"},
                "value": {"type": "string"},
                "category": {"type": "string",
                             "description": "identity | project | preference | system | general"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall",
        "description": ("Search stored facts. Most known facts are already in your system "
                        "prompt — use this only to search details that were truncated."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "forget",
        "description": "Delete a stored fact by key, when the user says it is wrong or outdated.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "system_info",
        "description": "Get OS, machine, current date/time, and free space on each drive.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

DISPATCH = {
    "create_folder": create_folder,
    "list_directory": list_directory,
    "search_files": search_files,
    "read_text_file": read_text_file,
    "write_text_file": write_text_file,
    "move_path": move_path,
    "delete_path": delete_path,
    "open_item": open_item,
    "run_powershell": run_powershell,
    "start_background_process": start_background_process,
    "read_process_log": read_process_log,
    "list_background_processes": list_background_processes,
    "stop_background_process": stop_background_process,
    "remember": remember,
    "recall": recall,
    "forget": forget,
    "check_environment": check_environment,
    "project_tree": project_tree,
    "inspect_project": inspect_project,
    "search_in_files": search_in_files,
    "check_url": check_url,
    "system_info": system_info,
}


def execute(name: str, args: dict) -> str:
    """Run a tool by name, converting any crash into a message the model can read."""
    fn = DISPATCH.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        return fn(**args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except PermissionError:
        return f"Permission denied. This may need FRIDAY to be run as Administrator."
    except Exception as e:
        return f"{name} failed: {type(e).__name__}: {e}"


# ---------------------------------------------------------------- tool groups

# Every tool schema is re-sent on EVERY API call. On a rate-limited free tier
# that is the single biggest source of token waste, so we ship only the tools
# the current job needs. Switch with '/mode' inside FRIDAY.
TOOL_GROUPS = {
    # everyday file wrangling — the default
    "core": ["create_folder", "list_directory", "search_files", "read_text_file",
             "write_text_file", "move_path", "delete_path", "open_item",
             "run_powershell", "system_info",
             "remember", "recall"],

    # auditing and running codebases
    "dev": ["list_directory", "read_text_file", "write_text_file", "open_item",
            "project_tree", "inspect_project", "search_in_files", "check_environment",
            "run_powershell", "start_background_process", "read_process_log",
            "list_background_processes", "stop_background_process", "check_url",
            "remember", "recall"],

    # read-only: cannot delete, write, or execute anything
    "safe": ["list_directory", "search_files", "read_text_file", "project_tree",
             "inspect_project", "search_in_files", "check_environment",
             "check_url", "system_info", "recall"],
}


def schemas_for(group: str) -> list[dict]:
    """Return only the schemas belonging to a group. 'all' returns everything."""
    if group == "all":
        return TOOL_SCHEMAS
    names = TOOL_GROUPS.get(group)
    if names is None:
        raise KeyError(group)
    return [s for s in TOOL_SCHEMAS if s["name"] in names]
