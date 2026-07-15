#!/usr/bin/env python3
"""Edit the /btw side-question system-reminder inside the Claude Code binary.

Auto-extracts the current reminder from the binary (no hardcoded version
text), so it keeps working across CLI updates. Always writes a patched COPY
(<binary>.btw-patched); the original is never modified.

Usage:
    btw-reminder.py show                 # print current reminder + byte budget
    btw-reminder.py edit                 # open reminder in $EDITOR, then patch
    btw-reminder.py set <file>           # patch using text from a file
    btw-reminder.py set -                # patch using text from stdin
    btw-reminder.py check <file>         # length-check only, no write
    btw-reminder.py rewrite "<intent>"   # local model (ollama) turns your rough
                                         # intent into a reminder, then patches
    btw-reminder.py rewrite -            # same, intent read from stdin

    Add --binary <path> to target a specific binary (default: `which claude`,
    symlink resolved). `show`/`edit` read from the ORIGINAL binary, so you
    always edit against the pristine text, not a previous patch.
"""

import os
import shutil
import subprocess
import sys
import tempfile

PREFIX = b"<system-reminder>This is a side question from the user."
SUFFIX = b"</system-reminder>"

OLLAMA_URL = os.environ.get("BTW_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("BTW_OLLAMA_MODEL", "")  # empty = first installed

REWRITE_SYSTEM = """You write a system-reminder for a coding assistant's "side question" mode.
Hard requirements that MUST appear in your output, whatever the user asks for:
- The model has NO tools: it cannot read files, run commands, or search.
- It is a one-off response with no follow-up turns.
- It must answer only from the conversation context, and say so if it doesn't know.
Then incorporate the user's stylistic/behavioral wishes.
Output ONLY the reminder text itself - no preamble, no quotes, no markdown fences.
It must be at most {budget} bytes of UTF-8; aim well under."""


def find_binary(argv):
    if "--binary" in argv:
        i = argv.index("--binary")
        path = argv[i + 1]
        del argv[i:i + 2]
    else:
        path = os.path.realpath(shutil.which("claude") or "")
    if not path or not os.path.isfile(path):
        sys.exit("Claude binary not found; pass --binary <path>.")
    return path


def extract(data):
    """Return (body_bytes, capacity). Body sits between PREFIX's tag and SUFFIX."""
    start = data.find(PREFIX)
    if start == -1:
        sys.exit("Reminder not found in binary - format changed; script needs updating.")
    body_start = start + len(b"<system-reminder>")
    end = data.find(SUFFIX, body_start)
    if end == -1 or end - body_start > 20000:
        sys.exit("Reminder terminator not found where expected; refusing to guess.")
    return data[body_start:end], end - body_start


def read_new_text(source):
    if source == "-":
        return sys.stdin.read()
    with open(source, encoding="utf-8") as f:
        return f.read()


def check(new_text, capacity):
    n = len(new_text.encode())
    if n > capacity:
        print(f"TOO LONG: {n} bytes, budget is {capacity} (over by {n - capacity}).")
        print("Shorten the text and retry.")
        return None
    print(f"OK: {n}/{capacity} bytes ({capacity - n} to spare).")
    return new_text.encode().ljust(capacity, b" ")


def patch(binary, data, old_body, new_body):
    count = data.count(old_body)
    out = binary + ".btw-patched"
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data.replace(old_body, new_body))
    shutil.copymode(binary, tmp)
    os.replace(tmp, out)  # atomic; works even while the old file is executing
    print(f"Patched {count} occurrence(s) -> {out}")
    print("Note: already-running claude-btw sessions keep the old text; restart them.")


def ollama_chat(messages):
    import json
    import urllib.request

    model = OLLAMA_MODEL
    if not model:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as r:
            tags = json.load(r)
        if not tags.get("models"):
            sys.exit("No ollama models installed.")
        model = tags["models"][0]["name"]
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({"model": model, "messages": messages, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        content = json.load(r)["message"]["content"]
    # strip <think> blocks reasoning models emit
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    return content.strip().strip("`").strip(), model


def rewrite(intent, capacity):
    messages = [
        {"role": "system", "content": REWRITE_SYSTEM.format(budget=capacity - 50)},
        {"role": "user", "content": intent},
    ]
    for attempt in range(4):
        text, model = ollama_chat(messages)
        n = len(text.encode())
        if n <= capacity:
            print(f"[{model}] draft OK: {n}/{capacity} bytes (attempt {attempt + 1})\n")
            return text
        print(f"[{model}] draft too long ({n}/{capacity}), asking it to shorten...")
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content": f"Too long: {n} bytes, limit {capacity - 50}. "
             "Rewrite shorter, keep the hard requirements."},
        ]
    sys.exit("Model couldn't fit the budget after 4 attempts. Try a simpler intent.")


def main():
    argv = sys.argv[1:]
    binary = find_binary(argv)
    if not argv:
        sys.exit(__doc__)
    cmd = argv[0]

    data = open(binary, "rb").read()
    body, capacity = extract(data)

    if cmd == "show":
        print(f"# binary: {binary}")
        print(f"# byte budget: {capacity}\n")
        print(body.decode(errors="replace").rstrip())

    elif cmd == "check":
        if len(argv) < 2:
            sys.exit("Usage: btw-reminder.py check <file>")
        check(read_new_text(argv[1]), capacity)

    elif cmd == "set":
        if len(argv) < 2:
            sys.exit("Usage: btw-reminder.py set <file|->")
        padded = check(read_new_text(argv[1]).rstrip("\n"), capacity)
        if padded is None:
            sys.exit(1)
        patch(binary, data, body, padded)

    elif cmd == "rewrite":
        if len(argv) < 2:
            sys.exit('Usage: btw-reminder.py rewrite "<intent>" (or - for stdin)')
        intent = sys.stdin.read() if argv[1] == "-" else " ".join(argv[1:])
        text = rewrite(intent, capacity)
        print(text)
        print()
        if input("Patch with this text? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted, nothing written.")
        padded = check(text, capacity)
        if padded is None:
            sys.exit(1)
        patch(binary, data, body, padded)

    elif cmd == "edit":
        editor = os.environ.get("EDITOR", "nano")
        with tempfile.NamedTemporaryFile(
            "w", suffix=".btw.txt", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(body.decode(errors="replace").rstrip() + "\n")
            tmp = tf.name
        try:
            print(f"# byte budget: {capacity}. Save and exit to patch; empty file aborts.")
            subprocess.call([editor, tmp])
            new_text = open(tmp, encoding="utf-8").read().rstrip("\n")
            if not new_text.strip():
                sys.exit("Empty text - aborted, nothing written.")
            padded = check(new_text, capacity)
            if padded is None:
                print(f"Your draft is saved at {tmp} - shorten it and run:")
                print(f"  {sys.argv[0]} set {tmp}")
                sys.exit(1)
            patch(binary, data, body, padded)
        finally:
            if os.path.exists(tmp) and "padded" in dir() and padded is not None:
                os.unlink(tmp)

    else:
        sys.exit(f"Unknown command: {cmd}\n\n{__doc__}")


if __name__ == "__main__":
    main()
