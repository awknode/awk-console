#!/usr/bin/env python3
"""Awknode Console connector: one door into FL Studio for any agent.

Two ways in, both standard-library Python with no installs:

  python awk_console.py call <cmd> ['{"json":"args"}']   one command, prints the reply
  python awk_console.py mcp                              MCP server on stdin/stdout

MCP is the shared language: Claude Code, Codex, Hermes and anything else that
speaks the Model Context Protocol launches the `mcp` mode as a stdio server
(locally, or over ssh from another machine) and gets the same tools.

It talks to the Awknode Console FL script through its io folder (the file
transport the Hermes FL Bridge proved on FL 2026). One request at a time,
guarded by a lock file, so several agents can share it safely.
"""

import json
import os
import sys
import time
import uuid

VERSION = "0.2.0"
HARDWARE_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Image-Line", "FL Studio",
                            "Settings", "Hardware")
# Every Awknode controller script has its own io folder (FL only allows file IO inside
# the script's own folder). The live one is whichever started its link most recently.
CONTROLLERS = ("AwknodeKeyLabEss", "AwknodeConsole")


def _pick_io():
    best, best_ts = None, -1.0
    for name in CONTROLLERS:
        d = os.path.join(HARDWARE_DIR, name, "io")
        try:
            with open(os.path.join(d, "console_status.json"), encoding="utf-8") as f:
                st = json.load(f)
            ts = float(st.get("ts", 0)) if st.get("ok") else -1.0
        except (OSError, ValueError, TypeError):
            ts = -1.0
        if best is None and os.path.isdir(d):
            best = d
        if ts > best_ts:
            best, best_ts = d, ts
    return best or os.path.join(HARDWARE_DIR, CONTROLLERS[-1], "io")


IO_DIR = os.environ.get("AWK_CONSOLE_IO") or _pick_io()
CMD = "console_cmd.json"
REP = "console_rep.json"
LOCK = "console_agent.lock"
TIMEOUT = float(os.environ.get("AWK_CONSOLE_TIMEOUT", "8"))


class ConsoleError(Exception):
    pass


# ---------------------------------------------------------------- transport

def _lock(deadline):
    path = os.path.join(IO_DIR, LOCK)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 30:  # holder died
                    os.replace(path, path + ".stale")
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise ConsoleError("another agent is mid-command (lock held); try again")
            time.sleep(0.05)


def call(cmd, args=None, timeout=TIMEOUT):
    """Send one command to FL; return its result or raise ConsoleError."""
    if not os.path.isdir(IO_DIR):
        raise ConsoleError("Awknode Console io folder not found: %s" % IO_DIR)
    req = dict(args or {})
    req["cmd"] = cmd
    req["id"] = uuid.uuid4().hex[:16]
    req["ts"] = time.time()
    deadline = time.time() + timeout
    lock = _lock(deadline)
    try:
        tmp = os.path.join(IO_DIR, CMD + ".agent.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(req, f)
        os.replace(tmp, os.path.join(IO_DIR, CMD))
        rep_path = os.path.join(IO_DIR, REP)
        while time.time() < deadline:
            time.sleep(0.03)
            try:
                with open(rep_path, "r", encoding="utf-8") as f:
                    rep = json.load(f)
            except (OSError, ValueError):
                continue
            if rep.get("id") == req["id"]:
                if rep.get("ok"):
                    return rep.get("result")
                raise ConsoleError(rep.get("error") or "FL returned an error")
        raise ConsoleError("no reply from FL in %.0fs: is FL open with Awknode Console "
                           "enabled in Options > MIDI settings?" % timeout)
    finally:
        try:
            os.replace(lock, lock + ".done")  # never delete; rename frees the lock
        except OSError:
            pass


# ---------------------------------------------------------------- MCP server

BRIEFING = """Awknode Console drives FL Studio and the user's MIDI hardware (KeyLab 61 etc).
Rules for any agent:
- Read before you write: fl_status, then list (mixer.list, channels.list, plugins.list, plugins.params).
- Plugin and mixer values are normalized 0..1. Use plugins.find_param to find a parameter by name.
- Wrap a group of changes: undo.point {name} first, so the user can Ctrl+Z it.
- Never start/stop playback or record unless the user asked.
- Plugins cannot be loaded or removed through FL's API: ask the user to do that.
- Hardware: hw.status shows the active profile; hw.seen shows the raw CCs the device sent;
  hw.learn binds controls by moving them; hw.map retargets one control; hw.events returns
  presses of buttons mapped to kind 'agent' (the user's 'do the thing' buttons).
- fl_help lists every command and its arguments.
- Macro (one call, fast): fl_call eq.cut {track: index|name, freq: Hz, order?: "Steep 4", band?: 1}
  = cut on the track's Parametric EQ 2: <=400 Hz high-pass (band 1), >400 Hz low-pass (band 7).
  Use it for any "cut <freq>" / "trim above <freq>".
- Studio macros (fl_help -> macros): mix.review / mix.preflight are read-only and only PROPOSE fixes.
  project.tidy / project.group / project.route_channels are dry runs until apply=true: show Stacks
  the plan, then apply. Applied groups are logged; project.undo puts them back exactly.
  Never rename something Stacks already named; his template's routing is deliberate.
- Notes: notes.paste writes a .mid + the piano-roll script 'Awknode Paste'; Stacks runs it from the
  channel's piano roll. audio.analyze / audio.melody work on files on the PC."""

TOOLS = [
    {"name": "fl_status",
     "description": "FL Studio + controller snapshot: connection, project, focused window, transport, tempo, hardware profile.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fl_help",
     "description": "List every Awknode Console command with its arguments.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fl_call",
     "description": "Run one Awknode Console command, e.g. cmd='mixer.list', or cmd='plugins.set_param_by_name' "
                    "with args {track, slot, query, value}. cmd='batch' with args {cmds:[...]} runs up to 64 in one go.",
     "inputSchema": {"type": "object", "properties": {
         "cmd": {"type": "string", "description": "command name from fl_help"},
         "args": {"type": "object", "description": "command arguments", "additionalProperties": True}},
         "required": ["cmd"]}},
    {"name": "hw_learn",
     "description": "Start learning hardware controls: the user then moves each listed control once, in order. "
                    "ids: list of control ids, or 'all' / 'unmapped'. Check progress with fl_call hw.status.",
     "inputSchema": {"type": "object", "properties": {
         "ids": {"description": "control ids, 'all' or 'unmapped'",
                 "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]}},
         "required": ["ids"]}},
    {"name": "hw_map",
     "description": "Point one hardware control at a new target. target.kind is one of focused_param{param}, "
                    "plugin_param{index,slot,param}, mixer_volume{offset}, mixer_pan{offset}, selected_volume, "
                    "selected_pan, transport{action}, bank{step}, agent{name}, none.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "target": {"type": "object", "additionalProperties": True},
         "mode": {"type": "string", "enum": ["abs", "rel64", "rel2c", "relsm", "button"]},
         "label": {"type": "string"}},
         "required": ["id"]}},
    {"name": "fl_mix_review",
     "description": "Read-only mix review: clipping, headroom, imbalance, low-end pan, missing EQ, "
                    "ungrouped families, solos. Proposes fixes (commands) but never applies them. "
                    "Level checks need FL playing.",
     "inputSchema": {"type": "object", "properties": {"ms": {"type": "integer", "default": 3000}}}},
    {"name": "fl_project_tidy",
     "description": "Project tidy-up: name default mixer tracks after their channel and colour tracks + "
                    "channels by family (drums red, 808 orange, keys amber, synth purple, strings indigo, "
                    "vox green, fx teal, sample brown, bus slate). Dry run unless apply=true; show Stacks "
                    "the plan first. Logged: fl_call project.undo puts it back exactly.",
     "inputSchema": {"type": "object", "properties": {
         "apply": {"type": "boolean", "default": False},
         "do": {"type": "array", "items": {"type": "string", "enum": ["names", "colors"]}},
         "palette": {"type": "object", "additionalProperties": {"type": "string"}},
         "keep_custom_colors": {"type": "boolean", "default": True},
         "only": {"type": "array", "items": {"type": "integer"}}}}},
    {"name": "fl_audio_analyze",
     "description": "Analyse an audio file on the PC (bounce or stem): LUFS, peak, crest, stereo "
                    "correlation, tempo candidates, estimated key, band balance.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "fl_notes_paste",
     "description": "Put notes into FL: writes a .mid and the piano-roll script 'Awknode Paste' with the "
                    "notes baked in. Stacks opens the channel's piano roll > scripts menu > Awknode Paste "
                    "(FL's API can't write notes from a controller script). Times are in beats.",
     "inputSchema": {"type": "object", "properties": {
         "notes": {"type": "array", "items": {"type": "object", "properties": {
             "pitch": {"type": "integer"}, "start": {"type": "number"},
             "length": {"type": "number"}, "velocity": {"type": "number"}},
             "required": ["pitch", "start", "length"]}},
         "name": {"type": "string"}, "bpm": {"type": "number"},
         "mode": {"type": "string", "enum": ["replace", "add"]}}, "required": ["notes"]}},
    {"name": "hw_events",
     "description": "Collect presses/moves of hardware controls mapped to kind 'agent' since the last call.",
     "inputSchema": {"type": "object", "properties": {"clear": {"type": "boolean", "default": True}}}},
]


TOOL_MACROS = {"fl_mix_review": "mix.review", "fl_project_tidy": "project.tidy",
               "fl_audio_analyze": "audio.analyze", "fl_notes_paste": "notes.paste"}


# ---- macros: multi-step jobs done here in one go (no agent round trips) ----------

EQ2_NAMES = ("fruity parametric eq 2", "parametric eq 2")
EQ_CUT_ORDER = "Gentle 4"  # Stacks' default high-pass slope (24 dB/oct); override with order


def _hz(text):
    t = str(text).strip().lower().replace(" ", "")
    mult = 1000.0 if "khz" in t else 1.0
    num = "".join(c for c in t if c in "0123456789.-")
    return float(num) * mult


def _mixer_track(arg):
    if isinstance(arg, int) or str(arg).isdigit():
        return int(arg)
    want = str(arg).strip().lower()
    tracks = call("mixer.list")
    hits = [t["index"] for t in tracks if t["name"].strip().lower() == want] or \
           [t["index"] for t in tracks if want in t["name"].strip().lower()]
    if len(hits) != 1:
        raise ConsoleError("track %r matched %d mixer tracks" % (arg, len(hits)))
    return hits[0]


def _set_choice(idx, slot, param, label, steps=48):
    """Step a choice param until its display text equals label (case-insensitive)."""
    want = label.strip().lower()
    for i in range(steps + 1):
        r = call("plugins.set_param", {"index": idx, "slot": slot, "param": param, "value": i / float(steps)})
        if str(r.get("value_str", "")).strip().lower() == want:
            return r
    raise ConsoleError("no option %r on param %d" % (label, param))


def _seek_hz(idx, slot, param, hz):
    lo, hi, r = 0.0, 1.0, None
    for _ in range(24):
        mid = (lo + hi) / 2.0
        r = call("plugins.set_param", {"index": idx, "slot": slot, "param": param, "value": mid})
        got = _hz(r["value_str"])
        if abs(got - hz) <= max(0.5, hz * 0.002):
            break
        lo, hi = (mid, hi) if got < hz else (lo, mid)
    return r


HIGH_CUT_ABOVE_HZ = 400.0  # Stacks: a cut above 400 Hz means trim the HIGH end


def eq_cut(a):
    """Cut on Parametric EQ 2: {track (index|name), freq Hz, order?, band?, slot?}.
    freq <= 400 Hz: high-pass on band 1 (low cut). Above 400 Hz: low-pass on band 7 (high cut)."""
    idx = _mixer_track(a["track"])
    hz = float(a["freq"])
    high = hz > HIGH_CUT_ABOVE_HZ
    band = int(a.get("band", 7 if high else 1))
    order = a.get("order") or EQ_CUT_ORDER
    slots = [s for s in call("plugins.list", {"track": idx})
             if s["name"].strip().lower() in EQ2_NAMES]
    if "slot" in a:
        slots = [s for s in slots if s["slot"] == int(a["slot"])]
    if not slots:
        raise ConsoleError("no Parametric EQ 2 on mixer track %d: Stacks needs to load one" % idx)
    slot = slots[0]["slot"]
    call("undo.point", {"name": "eq.cut %d Hz on track %d" % (hz, idx)})
    b = band - 1
    typ = _set_choice(idx, slot, 21 + b, "Low pass" if high else "High pass")
    frq = _seek_hz(idx, slot, 7 + b, hz)
    ordr = _set_choice(idx, slot, 28 + b, order)
    return {"track": idx, "slot": slot, "band": band, "type": typ["value_str"],
            "freq": frq["value_str"], "order": ordr["value_str"]}


MACROS = {"eq.cut": eq_cut}

# Studio macros (tidy-up, colours, bus layout, mix review, preflight) and audio/notes.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import awk_studio
    awk_studio.bind(lambda cmd, args=None, timeout=TIMEOUT: call(cmd, args, timeout), ConsoleError)
    MACROS.update(awk_studio.MACROS)
except ImportError as _exc:  # connector still works without the extras
    awk_studio = None
try:
    import awk_audio
    MACROS.update(awk_audio.MACROS)
    _AudioError = awk_audio.AudioError
except ImportError:
    awk_audio = None
    _AudioError = ConsoleError

MACRO_HELP = {
    "eq.cut": "track, freq Hz, order?, band? -> low/high cut on the track's Parametric EQ 2",
    "project.review": "read-only: unnamed tracks, loose channels, families, tidy plan size",
    "project.tidy": "apply?:false, do?:[names,colors], palette?:{family:#hex}, keep_custom_colors?:true, only?:[i]",
    "project.color": "color name|#hex, tracks?:[index|name|family], channels?:[...] (applies, logged)",
    "project.rename": "renames:[{type:mixer|channel, index, name}] (logged)",
    "project.group": "tracks:[...], bus: index|name, keep_master?:false, apply?:false -> bus layout",
    "project.route_channels": "channels?:[...], apply?:false -> channels on Master get own mixer tracks",
    "project.undo": "file? -> put back the last logged tidy/color/rename/group/route exactly",
    "project.changes": "limit? -> logged change groups",
    "mix.review": "ms?:3000 -> read-only findings + proposed fixes (press play for level checks)",
    "mix.preflight": "ms?:3000 -> pass/warn checklist before a bounce",
    "audio.analyze": "path -> LUFS, peak, crest, stereo, tempo candidates, key, band balance",
    "audio.melody": "path, bpm?, name? -> melody to .mid (best on a solo stem)",
    "notes.paste": "notes:[{pitch,start,length,velocity}] (beats), name?, bpm?, mode?:replace|add "
                   "-> .mid + piano-roll script 'Awknode Paste'",
}


def _tool(name, a):
    if name == "fl_status":
        out = {"connector": VERSION, "io": IO_DIR}
        for key, cmd in (("ping", "ping"), ("info", "info"), ("transport", "transport.status"),
                         ("tempo", "tempo.get"), ("hardware", "hw.status")):
            try:
                out[key] = call(cmd)
            except ConsoleError as exc:
                out[key] = {"error": str(exc)}
                if key == "ping":
                    break
        return out
    if name == "fl_help":
        out = call("help")
        out["macros"] = {k: v for k, v in MACRO_HELP.items() if k in MACROS}
        return out
    if name == "fl_call":
        if a["cmd"] in MACROS:
            return MACROS[a["cmd"]](a.get("args") or {})
        return call(a["cmd"], a.get("args") or {})
    if name in TOOL_MACROS:
        return MACROS[TOOL_MACROS[name]](a)
    if name == "hw_learn":
        return call("hw.learn", {"ids": a["ids"]})
    if name == "hw_map":
        args = {k: v for k, v in a.items() if k != "id"}
        args["control"] = a["id"]
        return call("hw.map", args)
    if name == "hw_events":
        return call("hw.events", {"clear": a.get("clear", True)})
    raise ConsoleError("unknown tool %r" % name)


def _reply(out, mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    out.write((json.dumps(msg) + "\n").encode("utf-8"))
    out.flush()


def handle(msg):
    """One JSON-RPC message -> (result, error) or None for notifications."""
    method = msg.get("method")
    if "id" not in msg:
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"protocolVersion": want,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "awknode-console", "version": VERSION},
                "instructions": BRIEFING}, None
    if method == "ping":
        return {}, None
    if method == "tools/list":
        return {"tools": TOOLS}, None
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            res = _tool(p.get("name"), p.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(res, indent=1)}], "isError": False}, None
        except (ConsoleError, _AudioError, KeyError, ValueError, TypeError, OSError) as exc:
            return {"content": [{"type": "text", "text": "error: %s" % exc}], "isError": True}, None
    return None, {"code": -32601, "message": "method not found: %s" % method}


def serve(inp=None, out=None):
    inp = inp or sys.stdin.buffer
    out = out or sys.stdout.buffer
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            _reply(out, None, error={"code": -32700, "message": "parse error"})
            continue
        r = handle(msg)
        if r is not None:
            _reply(out, msg.get("id"), *r)


def main(argv):
    if len(argv) >= 2 and argv[1] == "mcp":
        serve()
        return 0
    if len(argv) >= 3 and argv[1] == "call":
        raw = argv[3] if len(argv) > 3 else "{}"
        if raw.startswith("@"):  # @file.json: long args without shell quoting pain
            with open(raw[1:], encoding="utf-8") as f:
                raw = f.read()
        args = json.loads(raw)
        try:
            fn = MACROS.get(argv[2])
            print(json.dumps(fn(args) if fn else call(argv[2], args), indent=1))
            return 0
        except (ConsoleError, _AudioError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
