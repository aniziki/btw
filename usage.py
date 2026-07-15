#!/usr/bin/env python3
"""
usage.py — real-time model-routing / usage timeline for Claude Code sessions.

Watches every session + subagent transcript under ~/.claude/projects and prints,
per assistant turn, which model actually served it (server-echoed `model` field),
flagging refusal-fallback switches (Fable -> Opus) and tallying per-model usage.

This is a READ-ONLY log viewer. It reports what already happened; it does not
change, bypass, or influence any request or the safeguard that triggers a switch.
Purpose: understand which model you are actually billed for, turn by turn.

Note on fallback detection: Claude Code sends the switch decision to the API via
`x-cc-fallback-*` / `x-is-refusal-fallback` HTTP headers, which are NOT written to
the transcript. This tool detects the same turns from their in-log footprint:
a served-model change within a thread, corroborated by
`message.diagnostics.cache_miss_reason.type == "model_changed"`.

Usage:
  python3 usage.py                 # live tail (Ctrl-C to stop)
  python3 usage.py --once          # print recent timeline + summary, exit
  python3 usage.py --fallbacks     # only show switches/fallbacks + user turns
  python3 usage.py --filter scry   # only sources matching substring
  python3 usage.py --history 60    # past turns to show on start
  python3 usage.py --no-color
"""
import os, sys, json, time, glob, argparse, re
from collections import defaultdict
from datetime import datetime, timezone

ROOT_DEFAULT = os.path.expanduser("~/.claude/projects")
DESC_W = 40

# named topic filters for --topic (case-insensitive regex over turn content)
TOPIC_PRESETS = {
    "security": r"\b(xss|csrf|cve|vuln|exploit|payload|rce|sql ?inj|injection|"
                r"privilege|escalat|malware|cyber|attacker|sanitiz|shellcode|"
                r"poc|nightmare|rogueplanet|legacyhive|regloadkey|registry hive)\b",
}

MODEL_SHORT = {
    "claude-fable-5": "FABLE", "claude-mythos-5": "MYTHOS",
    "claude-opus-4-8": "OPUS", "claude-sonnet-5": "SONNET",
}
def model_short(m):
    if not m:
        return None
    if m in MODEL_SHORT:
        return MODEL_SHORT[m]
    if m.startswith("claude-haiku"):
        return "HAIKU"
    if m == "<synthetic>":
        return None
    return m.replace("claude-", "").upper()[:8]


class C:
    enabled = True
    def _w(code):
        return lambda s: (f"\033[{code}m{s}\033[0m" if C.enabled else s)
    dim = _w("2"); bold = _w("1"); green = _w("32"); yellow = _w("33")
    red = _w("1;31"); cyan = _w("36"); blue = _w("34"); grn_b = _w("1;32")

def model_color(short):
    return {"FABLE": C.green, "MYTHOS": C.green, "OPUS": C.yellow,
            "SONNET": C.cyan, "HAIKU": C.blue}.get(short, lambda s: s)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def clean_user_text(msg):
    c = msg.get("content"); parts = []
    if isinstance(c, str):
        parts = [c]
    elif isinstance(c, list):
        for p in c:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p["text"])
            elif isinstance(p, dict) and p.get("type") == "tool_result":
                return None
    txt = " ".join(parts).strip()
    if not txt or txt.startswith("<system-reminder>") or "<task-notification>" in txt:
        return None
    wrap = "The user sent a new message while you were working:"
    if wrap in txt:
        txt = txt.split(wrap, 1)[1]
    txt = txt.split("This is how Claude Code surfaces", 1)[0]
    return " ".join(txt.split()) or None


def tool_desc(part):
    inp = part.get("input", {}) or {}
    for k in ("query", "command", "pattern", "prompt", "description",
              "url", "file_path", "path"):
        if k in inp and inp[k]:
            return f"{part.get('name')}({str(inp[k])[:44].splitlines()[0]})"
    return part.get("name") or "tool"


def make_event(o):
    m = o.get("message")
    if not isinstance(m, dict):
        return None
    ts = parse_ts(o.get("timestamp", ""))
    cwd = o.get("cwd", "")
    project = os.path.basename(cwd) if cwd else o.get("sessionId", "")[:6]
    agent = o.get("attributionAgent"); agent_id = o.get("agentId")
    label = (agent or "sub") + "#" + agent_id[-4:] if agent_id else "main"
    source = f"{project}:{label}"
    thread = (o.get("sessionId"), agent_id or "main")

    role = m.get("role")
    if role == "user":
        txt = clean_user_text(m)
        if not txt:
            return None
        return dict(kind="U", ts=ts, source=source, thread=thread,
                    desc="user: " + txt[:80])
    if role != "assistant":
        return None
    short = model_short(m.get("model"))
    if not short:
        return None
    content = m.get("content") or []
    tools = [tool_desc(p) for p in content
             if isinstance(p, dict) and p.get("type") == "tool_use"]
    text = "".join(p.get("text", "") for p in content
                   if isinstance(p, dict) and p.get("type") == "text").strip()
    if tools:
        desc = "-> " + ", ".join(tools)
    elif text:
        desc = text[:DESC_W].replace("\n", " ")
    else:
        desc = f"({m.get('stop_reason') or '...'})"
    u = m.get("usage", {}) or {}
    cmr = (m.get("diagnostics", {}) or {}).get("cache_miss_reason", {}) or {}
    blob = (text + " " + " ".join(tools)).lower()
    is_fork = bool(o.get("isSidechain")) or agent == "fork" or bool(agent_id)
    return dict(kind="A", ts=ts, source=source, thread=thread, model=short,
                desc=desc, sr=m.get("stop_reason"), blob=blob,
                is_fork=is_fork, attr=(agent or "main"),
                out=u.get("output_tokens", 0), inp=u.get("input_tokens", 0),
                cache_r=u.get("cache_read_input_tokens", 0),
                cache_c=u.get("cache_creation_input_tokens", 0),
                model_changed=(cmr.get("type") == "model_changed"))


class Tally:
    """Per-model usage accumulator + fallback counter."""
    def __init__(self):
        self.m = defaultdict(lambda: dict(turns=0, out=0, inp=0, cr=0, cc=0))
        self.fallbacks = 0        # Fable -> non-Fable switches (refusal-fallback footprint)
        self.reverts = 0          # back-to-Fable switches

    def add(self, ev):
        d = self.m[ev["model"]]
        d["turns"] += 1; d["out"] += ev["out"]; d["inp"] += ev["inp"]
        d["cr"] += ev["cache_r"]; d["cc"] += ev["cache_c"]

    def summary(self):
        lines = [C.bold("── per-model usage (this view) ──")]
        for mdl, d in sorted(self.m.items(), key=lambda kv: -kv[1]["out"]):
            mc = model_color(mdl)
            lines.append(
                f"  {mc(mdl.ljust(7))} turns={d['turns']:<4} "
                f"out={d['out']:>8,}  in={d['inp']:>8,}  "
                f"cache_read={d['cr']:>9,}  cache_write={d['cc']:>7,}")
        lines.append(
            f"  {C.red('refusal-fallback switches (Fable→other)')}: {self.fallbacks}"
            f"   |   reverts to Fable: {self.reverts}")
        return "\n".join(lines)


def annotate(prev, cur, model_changed, tally):
    tag = C.dim(" [model_changed]") if model_changed else ""
    if prev is None or prev == cur:
        return tag if model_changed else ""
    if prev == "FABLE" and cur != "FABLE":
        tally.fallbacks += 1
        return C.red(f"  <== FALLBACK {prev}->{cur} (refusal-fallback footprint)") + tag
    if cur == "FABLE" and prev != "FABLE":
        tally.reverts += 1
        return C.grn_b(f"  <== reverted {prev}->{cur}") + tag
    return C.yellow(f"  <== switched {prev}->{cur}") + tag


def render(ev):
    t = ev["ts"].strftime("%H:%M:%S") if ev["ts"] else "--:--:--"
    src = ev["source"][:20].ljust(20)
    if ev["kind"] == "U":
        return f"{C.dim(t)}  {C.dim(src)}  {C.dim(ev['desc'])}"
    desc = ev["desc"]
    if len(desc) >= DESC_W:
        desc = desc[:DESC_W - 1] + "…"; lead = " "
    else:
        lead = " " + "." * (DESC_W - len(desc) - 1) + " "
    mc = model_color(ev["model"])
    toks = C.dim(f"{ev['out']:>5} out") if ev.get("out") else " " * 9
    return f"{t}  {C.dim(src)}  {desc}{lead}{mc(ev['model'].ljust(6))}  {toks}{ev.get('ann','')}"


def collect(offsets, root, filt):
    events = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        off = offsets.get(path, 0)
        if off > size:
            off = 0
        if off == size:
            continue
        try:
            with open(path, "rb") as f:
                f.seek(off); data = f.read(size - off)
        except OSError:
            continue
        nl = data.rfind(b"\n")
        if nl == -1:
            continue
        offsets[path] = off + nl + 1
        for raw in data[:nl + 1].split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            ev = make_event(o)
            if not ev:
                continue
            if filt and filt not in ev["source"] and filt not in str(ev["thread"][0]):
                continue
            events.append(ev)
    events.sort(key=lambda e: (e["ts"] or datetime.min.replace(tzinfo=timezone.utc)))
    return events


def event_matches(ev, flt):
    """Display filter. Assistant turns tested against model/forks/topic;
    user turns shown only when no audit filter is active."""
    if ev["kind"] != "A":
        return flt.get("trace") or not (flt["model"] or flt["forks_only"] or flt["topic_re"])
    if flt["model"] and ev["model"] != flt["model"]:
        return False
    if flt["forks_only"] and not ev.get("is_fork"):
        return False
    if flt["topic_re"] and not flt["topic_re"].search(ev.get("blob", "")):
        return False
    return True


def emit(ev, last_model, tally, filtered, flt, do_print):
    """Annotate + tally over ALL turns (keeps switch detection correct),
    then print + count into `filtered` only if it passes the display filters."""
    if ev["kind"] == "A":
        prev = last_model.get(ev["thread"])
        ev["ann"] = annotate(prev, ev["model"], ev["model_changed"], tally)
        last_model[ev["thread"]] = ev["model"]
        tally.add(ev)
        if flt.get("trace"):
            chg = " [CHG]" if ev.get("model_changed") else ""
            ev["ann"] = C.dim(f"  sr={ev.get('sr') or '-'}{chg}") + ev["ann"]
    else:
        ev["ann"] = ""
    if not do_print or not event_matches(ev, flt):
        return
    if flt["fallbacks_only"] and ev["kind"] == "A" and "==" not in ev["ann"]:
        return
    if ev["kind"] == "A":
        filtered.add(ev)
    print(render(ev))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT_DEFAULT)
    ap.add_argument("--filter", default="", help="substring match on source/session")
    ap.add_argument("--history", type=int, default=40)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--fallbacks", action="store_true",
                    help="only show model switches + user turns")
    ap.add_argument("--forks-only", action="store_true",
                    help="only fork/subagent turns")
    ap.add_argument("--model", default="",
                    help="only this served model (FABLE/OPUS/SONNET/HAIKU)")
    ap.add_argument("--topic", default="",
                    help="regex over turn content; preset name 'security' available")
    ap.add_argument("--audit", action="store_true",
                    help="shortcut: --forks-only --model FABLE --topic security --once")
    ap.add_argument("--trace", action="store_true",
                    help="per-turn detail: stop_reason + [CHG] flag + user turns "
                         "(best with --filter <session/agent> to scope one thread)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.audit:
        args.forks_only = True
        args.model = args.model or "FABLE"
        args.topic = args.topic or "security"
        args.once = True
        args.history = max(args.history, 100000)
    if args.no_color or not sys.stdout.isatty():
        C.enabled = False

    topic_pat = TOPIC_PRESETS.get(args.topic.lower(), args.topic) if args.topic else ""
    flt = dict(model=args.model.upper(), forks_only=args.forks_only,
               topic_re=re.compile(topic_pat, re.I) if topic_pat else None,
               fallbacks_only=args.fallbacks, trace=args.trace)
    active = any([flt["model"], flt["forks_only"], flt["topic_re"]])

    offsets, last_model, tally, filtered = {}, {}, Tally(), Tally()
    print(C.bold("Claude Code model-routing timeline") +
          C.dim(f"   root={args.root}  tz={datetime.now().astimezone().tzname()}"))
    if active:
        print(C.dim(f"  filters: model={flt['model'] or 'any'}  "
                    f"forks_only={flt['forks_only']}  topic={args.topic or 'any'}"))
    print(C.dim("time      source                request".ljust(72) + "model   out    switch"))
    print(C.dim("-" * 100))

    # History: annotate ALL in order (so switch detection is correct), print the
    # tail window that passes the filters.
    history = collect(offsets, args.root, args.filter)
    cut = max(0, len(history) - args.history)
    for i, ev in enumerate(history):
        emit(ev, last_model, tally, filtered, flt, do_print=(i >= cut))

    print()
    if active:
        n = sum(d["turns"] for d in filtered.m.values())
        print(C.bold(f"── filtered matches: {n} assistant turns ──"))
        print(filtered.summary())
    else:
        print(tally.summary())

    if args.once:
        return
    print(C.bold(C.green("\n---- live (Ctrl-C to stop) ----")))
    try:
        while True:
            time.sleep(args.interval)
            for ev in collect(offsets, args.root, args.filter):
                emit(ev, last_model, tally, filtered, flt, do_print=True)
    except KeyboardInterrupt:
        print("\n" + (filtered if active else tally).summary())
        print(C.dim("stopped."))


if __name__ == "__main__":
    main()
