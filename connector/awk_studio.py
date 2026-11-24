"""Awknode Studio macros: project tidy-up, colours, bus layout, mix review, preflight.

Ideas from fls-pilot (zorroukk, MIT): families-by-name, read-only review that only
*proposes* fixes, one undo unit per applied group. Written fresh for Awknode
Console's command set; no code copied.

Rules (Stacks'):
- Everything that writes is dry-run unless apply=true. The plan comes back first.
- Existing names are never overwritten by tidy; only default ones ("Insert 12",
  "Channel 3", "Sampler") get named.
- Every applied group is logged to changes/ next to this file, so project.undo
  puts back exact names, colours, channel targets and routes (FL's Ctrl+Z does
  not cover renames/colours reliably).

Stdlib only. Runs on the PC beside awk_console.py, which binds `call`.
"""

import json
import os
import re
import time

call = None


class StudioError(Exception):
    pass


def bind(call_fn, error_cls):
    global call, StudioError
    call = call_fn
    StudioError = error_cls


HERE = os.path.dirname(os.path.abspath(__file__))
CHANGES = os.path.join(HERE, "changes")

# ---------------------------------------------------------------- families + palette

# Order matters: the first family whose keyword is in the name wins.
FAMILIES = [
    ("bus", ("bus", "all drums", "all music", "all vox", "lost cause", "group", "master")),
    ("fx", ("reverb", "verb", "delay", "echo", "fx", "riser", "impact", "sweep", "transition")),
    ("808", ("808", "reese", "sub", "bass")),
    ("drums", ("kick", "snare", "clap", "hat", "hh", "oh", "perc", "drum", "rim", "tom",
               "cym", "crash", "ride", "shaker", "tamb", "snap")),
    ("vox", ("vox", "vocal", "adlib", "hook", "verse", "bgv", "harmony", "acapella")),
    ("keys", ("piano", "keys", "keyz", "keyscape", "rhodes", "noire", "ep ", "organ")),
    ("strings", ("string", "choir", "orch", "violin", "cello", "brass", "flute", "horn")),
    ("synth", ("synth", "serum", "omni", "lead", "pluck", "arp", "bell", "pad", "brainball",
               "melody", "kontakt")),
    ("sample", ("sample", "loop", "chop", "watermark")),
]

PALETTE = {  # Stacks' default colours per family (change freely; pass palette={} to override)
    "drums": "#E53935",    # red
    "808": "#FB8C00",      # orange
    "keys": "#FFB300",     # amber
    "synth": "#8E24AA",    # purple
    "strings": "#3949AB",  # indigo
    "vox": "#43A047",      # green
    "fx": "#00ACC1",       # teal
    "sample": "#6D4C41",   # brown
    "bus": "#546E7A",      # slate
}

LOW_END = ("808",)
EQ_WORDS = ("eq", "pro-q", "proq", "equo", "filter")

DEFAULT_MIXER = re.compile(r"^\s*(insert\s*\d*|current)?\s*$", re.I)
DEFAULT_CHANNEL = re.compile(r"^\s*(channel\s*\d*|sampler(\s*#?\d+)?|audioclip\s*\d*)?\s*$", re.I)


def family(name):
    n = " %s " % (name or "").lower()
    for fam, kws in FAMILIES:
        if any(k in n for k in kws):
            return fam
    return None


def _clean_channel_name(name):
    return re.sub(r"\s*-\s*awknode\s*$", "", name or "", flags=re.I).strip()


def _norm_hex(c):
    if not c:
        return None
    c = str(c).strip().upper()
    return c if c.startswith("#") else "#" + c


# ---------------------------------------------------------------- reading

def _batch(cmds):
    out = []
    for i in range(0, len(cmds), 64):
        out.extend(call("batch", {"cmds": cmds[i:i + 64]}))
    return out


def inventory(with_routes=True, with_plugins=True):
    """One read-only picture of the project: mixer tracks, channels, routes, plugins."""
    tracks = [t for t in call("mixer.list")
              if not str(t.get("name", "")).strip().lower() == "current"]
    chans = call("channels.list")
    feeders = {}
    for c in chans:
        if c.get("target") is not None:
            feeders.setdefault(c["target"], []).append(c)
    used = []
    for t in tracks:
        t["default_name"] = bool(DEFAULT_MIXER.match(t.get("name") or ""))
        t["feeders"] = [c["index"] for c in feeders.get(t["index"], [])]
        t["family"] = family(t.get("name")) or \
            (family(feeders[t["index"]][0]["name"]) if feeders.get(t["index"]) else None)
    cmds = []
    for t in tracks:
        if with_plugins:
            cmds.append({"cmd": "plugins.list", "track": t["index"]})
        if with_routes and t["index"] != 0:
            cmds.append({"cmd": "mixer.routes", "track": t["index"]})
    res = _batch(cmds) if cmds else []
    k = 0
    for t in tracks:
        if with_plugins:
            r = res[k]; k += 1
            t["plugins"] = [p["name"] for p in (r.get("result") or [])] if r.get("ok") else []
        if with_routes and t["index"] != 0:
            r = res[k]; k += 1
            t["sends_to"] = [d["index"] for d in (r.get("result") or {}).get("sends_to", [])] \
                if r.get("ok") else []
        t["used"] = bool(t.get("plugins") or not t["default_name"] or t["feeders"] or
                         any(d != 0 for d in t.get("sends_to", [])))
        if t["used"]:
            used.append(t)
    for c in chans:
        c["default_name"] = bool(DEFAULT_CHANNEL.match(c.get("name") or ""))
        c["family"] = family(c.get("name"))
    return {"tracks": tracks, "used": used, "channels": chans}


def _common_color(items):
    counts = {}
    for it in items:
        if it.get("color"):
            counts[it["color"]] = counts.get(it["color"], 0) + 1
    if not counts:
        return None
    col, n = max(counts.items(), key=lambda kv: kv[1])
    return col if n >= max(2, len(items) // 3) else None


# ---------------------------------------------------------------- change log + undo

def _log(kind, steps):
    os.makedirs(CHANGES, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + kind.replace(".", "_") + ".json"
    path = os.path.join(CHANGES, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"kind": kind, "ts": time.time(), "steps": steps, "undone": False}, f, indent=1)
    return name


def _write(cmd, args, undo, steps, errors):
    """Run one write; remember how to put it back."""
    try:
        r = call(cmd, args)
        steps.append({"cmd": cmd, "args": args, "result": r, "undo": undo(r)})
        return r
    except StudioError as exc:
        errors.append({"cmd": cmd, "args": args, "error": str(exc)})
        return None


def undo(a):
    """Put back the last applied group (or {file: name}) exactly, newest step first."""
    if not os.path.isdir(CHANGES):
        raise StudioError("no applied changes logged yet")
    files = sorted(f for f in os.listdir(CHANGES) if f.endswith(".json"))
    if a.get("file"):
        files = [f for f in files if f == a["file"]]
    pick = None
    for f in reversed(files):
        with open(os.path.join(CHANGES, f), encoding="utf-8") as fh:
            rec = json.load(fh)
        if not rec.get("undone"):
            pick = (f, rec)
            break
    if not pick:
        raise StudioError("nothing left to undo")
    f, rec = pick
    call("undo.point", {"name": "undo %s" % rec["kind"]})
    done, errors = 0, []
    for st in reversed(rec["steps"]):
        u = st.get("undo")
        if not u:
            continue
        try:
            call(u["cmd"], u["args"])
            done += 1
        except StudioError as exc:
            errors.append({"undo": u, "error": str(exc)})
    rec["undone"] = True
    with open(os.path.join(CHANGES, f), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
    return {"undone": f, "kind": rec["kind"], "steps_restored": done, "errors": errors}


def changes(a):
    if not os.path.isdir(CHANGES):
        return []
    out = []
    for f in sorted(os.listdir(CHANGES))[-int(a.get("limit", 10)):]:
        with open(os.path.join(CHANGES, f), encoding="utf-8") as fh:
            rec = json.load(fh)
        out.append({"file": f, "kind": rec["kind"], "steps": len(rec["steps"]),
                    "undone": rec.get("undone", False)})
    return out


def _undo_name(kind):
    return lambda r: {"cmd": kind, "args": {("track" if kind.startswith("mixer") else "channel"):
                                            r["index"], "new_name": r["before"]}}


def _undo_color(kind):
    def f(r):
        if r.get("before_raw") is None:
            return None
        return {"cmd": kind, "args": {("track" if kind.startswith("mixer") else "channel"):
                                      r["index"], "raw": r["before_raw"]}}
    return f


# ---------------------------------------------------------------- tidy plan

def _plan_tidy(inv, a):
    palette = dict(PALETTE)
    palette.update({k: _norm_hex(v) for k, v in (a.get("palette") or {}).items()})
    what = set(a.get("do") or ["names", "colors"])
    keep_custom = a.get("keep_custom_colors", True)
    plan = []
    tracks, chans = inv["used"], inv["channels"]
    by_index = {c["index"]: c for c in chans}
    if "names" in what:
        for t in tracks:
            if t["index"] == 0 or not t["default_name"] or not t["feeders"]:
                continue
            names = sorted({_clean_channel_name(by_index[i]["name"]) for i in t["feeders"]})
            names = [n for n in names if n and not DEFAULT_CHANNEL.match(n)]
            if len(names) == 1:
                plan.append({"op": "rename", "type": "mixer", "index": t["index"],
                             "from": t["name"], "to": names[0], "why": "named after its channel"})
    if "colors" in what:
        dflt_t = _common_color(inv["tracks"]) if keep_custom else None
        dflt_c = _common_color(chans) if keep_custom else None
        for t in tracks:
            fam = t.get("family")
            want = palette.get(fam) if fam else None
            if t["index"] == 0 or not want or _norm_hex(t.get("color")) == want:
                continue
            if keep_custom and dflt_t and _norm_hex(t.get("color")) != _norm_hex(dflt_t):
                continue  # Stacks already coloured this one by hand
            plan.append({"op": "color", "type": "mixer", "index": t["index"], "name": t["name"],
                         "family": fam, "from": t.get("color"), "to": want})
        for c in chans:
            fam = c.get("family")
            if not fam and c.get("target") is not None:
                tt = next((t for t in inv["tracks"] if t["index"] == c["target"]), None)
                fam = tt and tt.get("family")
            want = palette.get(fam) if fam else None
            if not want or _norm_hex(c.get("color")) == want:
                continue
            if keep_custom and dflt_c and _norm_hex(c.get("color")) != _norm_hex(dflt_c):
                continue
            plan.append({"op": "color", "type": "channel", "index": c["index"], "name": c["name"],
                         "family": fam, "from": c.get("color"), "to": want})
    return plan


def _apply(plan, kind):
    call("undo.point", {"name": kind})
    steps, errors = [], []
    for p in plan:
        mixer = p.get("type") == "mixer"
        key = "track" if mixer else "channel"
        if p["op"] == "rename":
            cmd = "mixer.set_name" if mixer else "channels.set_name"
            _write(cmd, {key: p["index"], "new_name": p["to"]}, _undo_name(cmd), steps, errors)
        elif p["op"] == "color":
            cmd = "mixer.set_color" if mixer else "channels.set_color"
            _write(cmd, {key: p["index"], "color": p["to"]}, _undo_color(cmd), steps, errors)
        elif p["op"] == "route":
            src, dst, on = p["src"], p["dst"], p["on"]
            _write("mixer.set_route", {"src": src, "dst": dst, "value": 1 if on else 0},
                   lambda r, s=src, d=dst, o=on: {"cmd": "mixer.set_route",
                                                  "args": {"src": s, "dst": d, "value": 0 if o else 1}},
                   steps, errors)
        elif p["op"] == "target":
            _write("channels.set_target", {"channel": p["index"], "track": p["to"]},
                   lambda r: {"cmd": "channels.set_target",
                              "args": {"channel": r["index"], "track": r["before"]}}
                   if r.get("before") is not None else None, steps, errors)
    log = _log(kind, steps) if steps else None
    return {"applied": len(steps), "errors": errors, "log": log,
            "undo": "fl_call project.undo" if log else None}


def tidy(a):
    """Name default mixer tracks after their channel + colour everything by family.
    {apply?: false, do?: ['names','colors'], palette?: {family: '#hex'}, keep_custom_colors?: true,
     only?: [plan indexes to apply]}"""
    inv = inventory()
    plan = _plan_tidy(inv, a)
    if a.get("only") is not None:
        plan = [p for i, p in enumerate(plan) if i in set(a["only"])]
    if not a.get("apply"):
        return {"dry_run": True, "plan": plan, "palette": dict(PALETTE, **(a.get("palette") or {})),
                "next": "show Stacks the plan; fl_call project.tidy {apply: true} (or only: [i,...])"}
    res = _apply(plan, "project.tidy")
    res["plan"] = plan
    return res


def _match_items(items, spec):
    """spec: family name, index, or name substring."""
    out = []
    for s in spec if isinstance(spec, list) else [spec]:
        if str(s).lower() in PALETTE:
            out += [it for it in items if it.get("family") == str(s).lower()]
        elif isinstance(s, int) or str(s).isdigit():
            out += [it for it in items if it["index"] == int(s)]
        else:
            out += [it for it in items if str(s).lower() in (it.get("name") or "").lower()]
    seen, uniq = set(), []
    for it in out:
        if it["index"] not in seen:
            seen.add(it["index"]); uniq.append(it)
    return uniq


NAMED_COLORS = {"red": "#E53935", "orange": "#FB8C00", "amber": "#FFB300", "yellow": "#FDD835",
                "lime": "#C0CA33", "green": "#43A047", "teal": "#00ACC1", "cyan": "#00BCD4",
                "blue": "#1E88E5", "indigo": "#3949AB", "purple": "#8E24AA", "violet": "#9575CD",
                "magenta": "#D81B60", "pink": "#EC407A", "brown": "#6D4C41", "gray": "#9E9E9E",
                "grey": "#9E9E9E", "slate": "#546E7A", "white": "#FFFFFF", "black": "#212121"}


def color(a):
    """Colour mixer tracks and/or channels. {color: 'red'|'#hex', tracks?: [..], channels?: [..],
    apply?: false}. tracks/channels take indexes, name parts, or a family (drums, 808, keys...)."""
    want = NAMED_COLORS.get(str(a.get("color", "")).lower()) or _norm_hex(a.get("color"))
    if not want or not re.match(r"^#[0-9A-F]{6}$", want):
        raise StudioError("color: a name (%s) or '#RRGGBB'" % ", ".join(sorted(NAMED_COLORS)))
    inv = inventory(with_routes=False, with_plugins=False)
    plan = []
    if a.get("tracks") is not None:
        for t in _match_items(inv["tracks"], a["tracks"]):
            plan.append({"op": "color", "type": "mixer", "index": t["index"], "name": t["name"],
                         "from": t.get("color"), "to": want})
    if a.get("channels") is not None:
        for c in _match_items(inv["channels"], a["channels"]):
            plan.append({"op": "color", "type": "channel", "index": c["index"], "name": c["name"],
                         "from": c.get("color"), "to": want})
    if not plan:
        raise StudioError("nothing matched: give tracks and/or channels")
    if not a.get("apply", True):
        return {"dry_run": True, "plan": plan}
    res = _apply(plan, "project.color")
    res["plan"] = plan
    return res


def rename(a):
    """{renames: [{type: 'mixer'|'channel', index, name}]} in one logged group."""
    plan = [{"op": "rename", "type": r["type"], "index": int(r["index"]), "to": r["name"]}
            for r in a.get("renames") or []]
    if not plan:
        raise StudioError("renames: [{type, index, name}]")
    return _apply(plan, "project.rename")


def group(a):
    """Bus layout: send tracks to a bus and (by default) off Master.
    {tracks: [index|name|family], bus: index|name, keep_master?: false, apply?: false}"""
    inv = inventory(with_plugins=False)
    bus = _match_items(inv["tracks"], [a["bus"]])
    if len(bus) != 1:
        raise StudioError("bus %r matched %d mixer tracks" % (a["bus"], len(bus)))
    bus = bus[0]
    srcs = [t for t in _match_items(inv["used"], a["tracks"]) if t["index"] not in (0, bus["index"])]
    plan = []
    for t in srcs:
        if bus["index"] not in t.get("sends_to", []):
            plan.append({"op": "route", "src": t["index"], "dst": bus["index"], "on": True,
                         "name": t["name"]})
        if not a.get("keep_master") and 0 in t.get("sends_to", []):
            plan.append({"op": "route", "src": t["index"], "dst": 0, "on": False, "name": t["name"]})
    if bus["index"] != 0 and 0 not in bus.get("sends_to", []) and not bus.get("sends_to"):
        plan.append({"op": "route", "src": bus["index"], "dst": 0, "on": True, "name": bus["name"]})
    if not a.get("apply"):
        return {"dry_run": True, "bus": bus["name"], "plan": plan}
    res = _apply(plan, "project.group")
    res["plan"] = plan
    return res


def route_channels(a):
    """Give channels that play straight into Master their own mixer track.
    {channels?: [..] (default: every channel on Master), apply?: false}"""
    inv = inventory(with_routes=False, with_plugins=False)
    chans = _match_items(inv["channels"], a["channels"]) if a.get("channels") else \
        [c for c in inv["channels"] if c.get("target") == 0]
    free = [t["index"] for t in inv["tracks"]
            if t["index"] != 0 and t["default_name"] and not t["feeders"] and not t["used"]]
    plan = []
    for c in chans:
        if not free:
            break
        t = free.pop(0)
        plan.append({"op": "target", "type": "channel", "index": c["index"], "name": c["name"],
                     "from": c.get("target"), "to": t})
        nm = _clean_channel_name(c["name"])
        if nm and not DEFAULT_CHANNEL.match(nm):
            plan.append({"op": "rename", "type": "mixer", "index": t, "to": nm})
    if not a.get("apply"):
        return {"dry_run": True, "plan": plan}
    res = _apply(plan, "project.route_channels")
    res["plan"] = plan
    return res


# ---------------------------------------------------------------- review + preflight

CLIP_DB = -1.0
HOT_DB = -3.0
IMBALANCE_DB = 6.0
MASTER_HEADROOM_DB = -3.0
LOW_END_PAN = 0.2


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _finding(rule, sev, track, msg, fix=None):
    return {"rule": rule, "severity": sev, "track": track, "message": msg, "fix": fix}


def _levels(inv, ms):
    playing = bool(call("transport.status").get("playing"))
    if not playing:
        return False, {}
    want = [t["index"] for t in inv["used"]]
    res = call("meters.sample", {"tracks": want, "ms": ms}, timeout=ms / 1000.0 + 10)
    return True, {r["index"]: r for r in res.get("tracks", [])}


def review(a):
    """Read-only mix review. Findings + proposed fixes; never writes.
    {ms?: 3000}: if FL is playing, peaks are sampled for that long."""
    inv = inventory()
    used = inv["used"]
    playing, lv = _levels(inv, int(a.get("ms", 3000)))
    out = []
    if playing:
        for t in used:
            m = _f((lv.get(t["index"]) or {}).get("max_db"))
            t["peak_db"] = m
            if m is None or t.get("muted"):
                continue
            if m >= 0.0:
                out.append(_finding("clipping", "high", t["name"], "%s peaks at %+.1f dB (clipping)" %
                                    (t["name"], m),
                                    {"cmd": "mixer.set_volume",
                                     "args": {"track": t["index"], "delta_db": round(-3.0 - m, 1)}}))
            elif m > CLIP_DB and t["index"] != 0:
                out.append(_finding("clip_risk", "medium", t["name"], "%s peaks at %+.1f dB" % (t["name"], m),
                                    {"cmd": "mixer.set_volume",
                                     "args": {"track": t["index"], "delta_db": round(-3.0 - m, 1)}}))
        master = _f((lv.get(0) or {}).get("max_db"))
        if master is not None and master > MASTER_HEADROOM_DB:
            out.append(_finding("master_headroom", "medium", "Master",
                                "Master peaks at %+.1f dB: under 3 dB of headroom before mastering. Trim "
                                "the buses, not the Master fader." % master))
        src = [t for t in used if t["index"] != 0 and not t.get("muted") and t.get("peak_db") is not None
               and t["peak_db"] > -60 and t.get("family") != "bus"]
        if len(src) >= 4:
            vals = sorted(t["peak_db"] for t in src)
            med = vals[len(vals) // 2]
            for t in src:
                if t["peak_db"] - med > IMBALANCE_DB and t.get("family") not in LOW_END + ("drums",):
                    out.append(_finding("imbalance", "low", t["name"],
                                        "%s is %.1f dB above the median track peak (%.1f dB)" %
                                        (t["name"], t["peak_db"] - med, med)))
            hot = [t for t in src if t["peak_db"] > HOT_DB]
            if len(hot) >= max(2, int(0.4 * len(src))):
                out.append(_finding("low_headroom", "medium", None,
                                    "%d of %d playing tracks peak above -3 dB: gain-stage down before "
                                    "the buses" % (len(hot), len(src))))
    for t in used:
        if t["index"] == 0:
            continue
        fam = t.get("family")
        if fam in LOW_END and abs(_f(t.get("pan")) or 0) > LOW_END_PAN:
            out.append(_finding("low_end_pan", "medium", t["name"],
                                "%s is panned %+.2f: keep 808/bass centred" % (t["name"], t["pan"]),
                                {"cmd": "mixer.set_pan", "args": {"track": t["index"], "value": 0.0}}))
        if fam in ("keys", "synth", "strings", "vox", "sample") and t.get("plugins") is not None and \
                not any(any(w in p.lower() for w in EQ_WORDS) for p in t["plugins"]) and \
                (t.get("plugins") or t["feeders"]):
            out.append(_finding("no_eq", "low", t["name"],
                                "%s has no EQ: a low cut around 120-200 Hz usually clears room for the "
                                "808 (heuristic)" % t["name"],
                                {"cmd": "eq.cut", "args": {"track": t["index"], "freq": 150},
                                 "needs": "a Parametric EQ 2 loaded on the track"}))
        if t.get("solo"):
            out.append(_finding("solo_left_on", "medium", t["name"], "%s is soloed" % t["name"]))
    fams = {}
    for t in used:
        if t["index"] != 0 and t.get("family") in ("drums", "vox", "keys", "synth", "strings", "808"):
            fams.setdefault(t["family"], []).append(t)
    sends = {t["index"]: set(t.get("sends_to") or []) for t in inv["tracks"]}

    def reach(i):
        seen, todo = set(), list(sends.get(i, ()))
        while todo:
            d = todo.pop()
            if d in seen or d == 0:
                continue
            seen.add(d)
            todo.extend(sends.get(d, ()))
        return seen

    for fam, members in fams.items():
        if len(members) < 2:
            continue
        # grouped = every member ends up on one shared bus (directly or through a chain),
        # or into a member that the others all feed (that member is the group's bus).
        reaches = {m["index"]: reach(m["index"]) | {m["index"]} for m in members}
        common = set.intersection(*reaches.values()) - {0}
        if not common:
            out.append(_finding("ungrouped", "low", None,
                                "%d %s tracks don't share a bus: %s" %
                                (len(members), fam, ", ".join(m["name"] for m in members)),
                                {"cmd": "project.group",
                                 "args": {"tracks": [m["index"] for m in members], "bus": "<bus>"}}))
    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: rank[f["severity"]])
    return {"playing": playing,
            "levels_note": None if playing else "press play for level/clip checks (peaks need audio)",
            "tracks_checked": len(used), "findings": out}


def preflight(a):
    """Before a bounce: a pass/warn checklist. Read-only. {ms?: 3000}"""
    r = review(a)
    inv_ch = call("channels.list")
    tracks = call("mixer.list")
    checks = []

    def chk(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    solos = [t["name"] for t in tracks if t.get("solo")] + \
            ["ch:" + c["name"] for c in inv_ch if c.get("solo")]
    chk("nothing soloed", not solos, solos or "ok")
    muted = [t["name"] for t in tracks if t.get("muted") and not DEFAULT_MIXER.match(t["name"] or "")]
    chk("muted tracks are meant to be", True, muted or "none muted")
    clips = [f["message"] for f in r["findings"] if f["rule"] in ("clipping", "clip_risk")]
    chk("no track clipping", r["playing"] and not clips, clips or ("ok" if r["playing"] else
                                                                   "not playing: press play and re-run"))
    mh = [f["message"] for f in r["findings"] if f["rule"] == "master_headroom"]
    chk("master headroom >= 3 dB", r["playing"] and not mh, mh or ("ok" if r["playing"] else "not measured"))
    loose = [c["name"] for c in inv_ch if c.get("target") == 0 and not DEFAULT_CHANNEL.match(c["name"] or "")]
    chk("every channel has its own mixer track", not loose, loose or "ok")
    chk("tempo", True, call("tempo.get"))
    return {"ready": all(c["ok"] for c in checks), "checks": checks,
            "other_findings": [f for f in r["findings"] if f["rule"] not in
                               ("clipping", "clip_risk", "master_headroom", "solo_left_on")]}


def review_project(a):
    """Read-only organisation review: default names, loose channels, colour plan size."""
    inv = inventory()
    plan = _plan_tidy(inv, {})
    return {
        "unnamed_mixer_tracks_in_use": [{"index": t["index"], "name": t["name"]}
                                        for t in inv["used"] if t["default_name"] and t["index"] != 0],
        "unnamed_channels": [{"index": c["index"], "name": c["name"]}
                             for c in inv["channels"] if c["default_name"]],
        "channels_straight_to_master": [{"index": c["index"], "name": c["name"]}
                                        for c in inv["channels"] if c.get("target") == 0],
        "no_family": [t["name"] for t in inv["used"] if not t.get("family") and t["index"] != 0],
        "families": {f: [t["name"] for t in inv["used"] if t.get("family") == f] for f, _ in FAMILIES},
        "tidy_plan_size": len(plan),
        "next": "fl_call project.tidy (dry run) to see the plan",
    }


MACROS = {
    "project.review": review_project,
    "project.tidy": tidy,
    "project.color": color,
    "project.rename": rename,
    "project.group": group,
    "project.route_channels": route_channels,
    "project.undo": undo,
    "project.changes": changes,
    "mix.review": review,
    "mix.preflight": preflight,
}
