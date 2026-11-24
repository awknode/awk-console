# name=Awknode Console
r"""Awknode Console — agent + hardware control surface for FL Studio.

Cloned 2026-11-18 from the Hermes FL Bridge v2.2 (left untouched in
Settings\Hardware\HermesFLBridge). Same JSON command set, its own io folder
and port, plus a hardware layer (hw.*): a JSON profile maps the knobs,
faders and buttons of the MIDI device this script is bound to.

Original notes:

Runs as an FL Studio MIDI device script inside FL's embedded CPython 3.12.
Threading contract: the UDP receiver thread ONLY recvfrom -> parse -> queue.
FL API modules (mixer/channels/plugins/ui/transport/general) are called ONLY
from the script thread (OnInit / OnIdle / OnDeInit).
MIDI pass-through: OnMidiIn/OnMidiMsg never set event.handled — hardware gear
plugged into the bound interface keeps working normally.
"""

import json
import math
import os
import queue
import socket
import sys
import threading
import time
import io as _io_mod

import channels
import general
import mixer
import plugins
import transport
import ui

try:
    import midi  # REC event ids (FX slot bypass)
except ImportError:
    midi = None

try:
    import _winapi  # Windows-only raw Win32 layer (no _io/_socket/_ctypes)
except ImportError:
    _winapi = None

HOST = "127.0.0.1"
PORT = 8766
MAX_PER_TICK = 16
MAX_REPLY_BYTES = 60000

# File-based fallback transport (used when FL's embedded Python refuses sockets).
# FL 2026's script sandbox confines file IO to the Documents user-data tree:
# AppData paths are rejected outright (DIAG3 2026-11-16: read/write/create all
# FAIL in AppData, all OK in the script folder). os.unlink is also refused, so
# the cmd file is never deleted — the request-id dedup guard prevents re-runs.
FILE_DIR = r"C:\Users\quick\Documents\Image-Line\FL Studio\Settings\Hardware\AwknodeConsole\io"
CMD_NAME = "console_cmd.json"
REP_NAME = "console_rep.json"
STATUS_NAME = "console_status.json"
STALE_SECONDS = 60
RETRY_BACKOFF_SECONDS = 60  # a dead transport context must not spam every idle tick

_stop = threading.Event()
_q = queue.Queue()
_sock = None
_thread = None
_mode = None  # "udp" or "file"
_io_mode = None  # "builtin", "rawfd" or "ctypes" (file transport backend)
_started = False
_last_err = None
_next_retry_at = 0.0
_fail_sig = None
_last_cmd_id = None


# ---------- helpers ----------

def _db(x):
    """JSON-safe dB value (strict JSON has no Infinity)."""
    try:
        fx = float(x)
    except (TypeError, ValueError):
        return x
    if fx == float("-inf"):
        return "-inf"
    if fx == float("inf"):
        return "inf"
    if math.isnan(fx):
        return None
    return round(fx, 3)


def _safe_to_edit():
    fn = getattr(general, "safeToEdit", None)
    if callable(fn):
        try:
            return fn() != 0
        except Exception:
            return True
    return True


def _require_editable():
    if not _safe_to_edit():
        raise RuntimeError("not-safe-to-edit")


def _track_index(req):
    if "track" in req:
        i = int(req["track"])
        if not (0 <= i < mixer.trackCount()):
            raise ValueError("track index out of range: %d" % i)
        return i
    name = str(req.get("name", "")).strip().lower()
    if not name:
        raise ValueError("need 'track' or 'name'")
    matches = [i for i in range(mixer.trackCount())
               if mixer.getTrackName(i).strip().lower() == name]
    if not matches:
        raise ValueError("no mixer track named %r" % req.get("name"))
    if len(matches) > 1:
        raise ValueError("ambiguous name %r: indices %s" % (req.get("name"), matches))
    return matches[0]


def _track_state(i):
    return {
        "index": i,
        "name": mixer.getTrackName(i),
        "vol_norm": round(mixer.getTrackVolume(i, 0), 6),
        "vol_db": _db(mixer.getTrackVolume(i, 1)),
        "pan": round(mixer.getTrackPan(i), 4),
        "muted": bool(mixer.isTrackMuted(i)),
        "solo": bool(mixer.isTrackSolo(i)),
        "color": _color_hex(_try(mixer, "getTrackColor", i)),
    }


def _param_value_string(param, index, slot):
    try:
        return plugins.getParamValueString(param, index, slot)
    except Exception:
        return None


# ---------- commands ----------

def _cmd_ping(req):
    out = {"pong": True, "api_version": general.getVersion(), "build": BUILD}
    fn = getattr(general, "getProjectTitle", None)
    if callable(fn):
        try:
            out["project"] = fn()
        except Exception:
            out["project"] = None
    return out


def _cmd_info(req):
    out = {}
    for key, fn in (("focused_caption", getattr(ui, "getFocusedFormCaption", None)),
                    ("focused_plugin", getattr(ui, "getFocusedPluginName", None))):
        if callable(fn):
            try:
                out[key] = fn()
            except Exception:
                out[key] = None
    return out


def _cmd_mixer_list(req):
    n = mixer.trackCount()
    off = int(req.get("offset", 0))
    lim = int(req.get("limit", n))
    return [_track_state(i) for i in range(max(0, off), min(n, off + lim))]


def _cmd_mixer_get(req):
    return _track_state(_track_index(req))


def _seek_db(setter, getter_db, target):
    """FL's fader taper is undocumented: binary-search the normalized value against the
    API's own dB readback. Readback is truth."""
    if target <= -100:
        setter(0.0)
        return
    lo, hi = 0.0, 1.0
    for _ in range(22):
        mid = (lo + hi) / 2.0
        setter(mid)
        db = getter_db()
        if db == float("-inf"):
            lo = mid
            continue
        if abs(db - target) <= 0.05:
            break
        if db < target:
            lo = mid
        else:
            hi = mid


def _cmd_mixer_set_volume(req):
    _require_editable()
    i = _track_index(req)
    if "delta_db" in req:  # relative: "2 dB quieter" = delta_db -2
        cur = mixer.getTrackVolume(i, 1)
        if cur == float("-inf"):
            raise ValueError("track is at -inf dB; set an absolute value instead")
        _seek_db(lambda v: mixer.setTrackVolume(i, v, 0), lambda: mixer.getTrackVolume(i, 1),
                 cur + float(req["delta_db"]))
        return _track_state(i)
    unit = req.get("unit", "norm")
    value = float(req["value"])
    if unit == "norm":
        mixer.setTrackVolume(i, min(1.0, max(0.0, value)), 0)
    elif unit == "db":
        if value <= -100:
            mixer.setTrackVolume(i, 0.0, 0)
        else:
            # FL's fader taper is undocumented: binary-search the normalized
            # value against the API's own dB readback. Readback is truth.
            lo, hi = 0.0, 1.0
            for _ in range(20):
                mid = (lo + hi) / 2.0
                mixer.setTrackVolume(i, mid, 0)
                db = mixer.getTrackVolume(i, 1)
                if db == float("-inf"):
                    lo = mid
                    continue
                if abs(db - value) <= 0.1:
                    break
                if db < value:
                    lo = mid
                else:
                    hi = mid
    else:
        raise ValueError("unit must be 'norm' or 'db'")
    return _track_state(i)


def _cmd_mixer_set_pan(req):
    _require_editable()
    i = _track_index(req)
    v = min(1.0, max(-1.0, float(req["value"])))
    mixer.setTrackPan(i, v, 0)
    return {"index": i, "pan": mixer.getTrackPan(i)}


def _cmd_mixer_mute(req):
    _require_editable()
    i = _track_index(req)
    mixer.muteTrack(i, int(req.get("value", -1)))
    return {"index": i, "muted": bool(mixer.isTrackMuted(i))}


def _cmd_mixer_solo(req):
    _require_editable()
    i = _track_index(req)
    mixer.soloTrack(i, int(req.get("value", -1)))
    return {"index": i, "solo": bool(mixer.isTrackSolo(i))}


def _cmd_mixer_select(req):
    i = _track_index(req)
    mixer.setTrackNumber(i, 1)
    return {"index": i, "name": mixer.getTrackName(i)}


def _cmd_mixer_open_effect(req):
    track = int(req["track"])
    slot = int(req["slot"])
    mixer.focusEditor(track, slot)
    return {"track": track, "slot": slot}


def _cmd_mixer_peaks(req):
    i = _track_index(req)
    return {"index": i, "peak": mixer.getTrackPeaks(i, 2)}


def _cmd_channels_list(req):
    n = channels.channelCount(1)
    off = int(req.get("offset", 0))
    lim = int(req.get("limit", n))
    out = []
    for i in range(max(0, off), min(n, off + lim)):
        out.append({
            "index": i,
            "name": channels.getChannelName(i, True),
            "vol_norm": round(channels.getChannelVolume(i, 0, True), 6),
            "vol_db": _db(channels.getChannelVolume(i, 1, True)),
            "muted": bool(channels.isChannelMuted(i, True)),
            "solo": bool(channels.isChannelSolo(i, True)),
            "type": channels.getChannelType(i, True),
            "color": _color_hex(_ch_try("getChannelColor", i)),
            "target": _ch_try("getTargetFxTrack", i),
        })
    return out


def _channel_index(req):
    if "channel" in req:
        return int(req["channel"])
    name = str(req.get("name", "")).strip().lower()
    if not name:
        raise ValueError("need 'channel' (index) or 'name'")
    hits = [i for i in range(channels.channelCount(True))
            if channels.getChannelName(i, True).strip().lower() == name]
    if len(hits) != 1:
        hits = [i for i in range(channels.channelCount(True))
                if name in channels.getChannelName(i, True).strip().lower()]
    if len(hits) != 1:
        raise ValueError("channel name %r matched %d channels" % (req.get("name"), len(hits)))
    return hits[0]


def _cmd_channels_set_volume(req):
    _require_editable()
    i = _channel_index(req)
    if "delta_db" in req or req.get("unit") == "db":
        cur = channels.getChannelVolume(i, 1, True)
        target = (cur + float(req["delta_db"])) if "delta_db" in req else float(req["value"])
        if "delta_db" in req and cur == float("-inf"):
            raise ValueError("channel is at -inf dB; set an absolute value instead")
        _seek_db(lambda v: channels.setChannelVolume(i, v, 0, True),
                 lambda: channels.getChannelVolume(i, 1, True), target)
        return {"index": i, "name": channels.getChannelName(i, True),
                "vol_norm": channels.getChannelVolume(i, 0, True),
                "vol_db": _db(channels.getChannelVolume(i, 1, True))}
    v = min(1.0, max(0.0, float(req["value"])))
    channels.setChannelVolume(i, v, 0, True)
    return {"index": i, "vol_norm": channels.getChannelVolume(i, 0, True),
            "vol_db": _db(channels.getChannelVolume(i, 1, True))}


def _cmd_channels_mute(req):
    _require_editable()
    i = int(req["channel"])
    channels.muteChannel(i, int(req.get("value", -1)), True)
    return {"index": i, "muted": bool(channels.isChannelMuted(i, True))}


def _cmd_channels_solo(req):
    _require_editable()
    i = int(req["channel"])
    channels.soloChannel(i, int(req.get("value", -1)), True)
    return {"index": i, "solo": bool(channels.isChannelSolo(i, True))}


def _cmd_channels_open_editor(req):
    i = int(req["channel"])
    show = int(req.get("show", 1))
    channels.showEditor(i, show, True)
    return {"index": i, "show": show}


def _cmd_plugins_list(req):
    if "track" in req:
        idx = int(req["track"])
        return [{"index": idx, "slot": s, "name": plugins.getPluginName(idx, s)}
                for s in range(10) if plugins.isValid(idx, s)]
    if "channel" in req:
        idx = int(req["channel"])
        if plugins.isValid(idx, -1):
            return [{"index": idx, "slot": -1, "name": plugins.getPluginName(idx, -1)}]
        return []
    raise ValueError("need 'track' or 'channel'")


def _cmd_plugins_params(req):
    idx = int(req["index"])
    slot = int(req.get("slot", -1))
    count = plugins.getParamCount(idx, slot)
    off = int(req.get("offset", 0))
    lim = int(req.get("limit", count))
    params = []
    for p in range(max(0, off), min(count, off + lim)):
        params.append({
            "param": p,
            "name": plugins.getParamName(p, idx, slot),
            "value": plugins.getParamValue(p, idx, slot),
            "value_str": _param_value_string(p, idx, slot),
        })
    return {"count": count, "params": params}


def _cmd_plugins_get_param(req):
    idx = int(req["index"])
    slot = int(req.get("slot", -1))
    p = int(req["param"])
    return {"value": plugins.getParamValue(p, idx, slot),
            "value_str": _param_value_string(p, idx, slot)}


def _cmd_plugins_set_param(req):
    _require_editable()
    idx = int(req["index"])
    slot = int(req.get("slot", -1))
    p = int(req["param"])
    v = min(1.0, max(0.0, float(req["value"])))
    plugins.setParamValue(v, p, idx, slot, 0)  # NOTE: value comes FIRST
    return {"value": plugins.getParamValue(p, idx, slot),
            "value_str": _param_value_string(p, idx, slot)}


def _cmd_plugins_preset(req):
    _require_editable()
    idx = int(req["index"])
    slot = int(req.get("slot", -1))
    d = req.get("dir", "next")
    if d == "next":
        plugins.nextPreset(idx, slot)
    elif d == "prev":
        plugins.prevPreset(idx, slot)
    else:
        raise ValueError("dir must be 'next' or 'prev'")
    return {"index": idx, "slot": slot, "dir": d}


def _cmd_transport_start(req):
    transport.start()
    return {"playing": bool(transport.isPlaying())}


def _cmd_transport_stop(req):
    transport.stop()
    return {"playing": bool(transport.isPlaying())}


def _cmd_transport_status(req):
    return {"playing": bool(transport.isPlaying())}


def _cmd_tempo_get(req):
    return {"bpm": mixer.getCurrentTempo()}


def _cmd_tempo_set(req):
    _require_editable()
    bpm = min(999.0, max(20.0, float(req["bpm"])))
    mixer.setCurrentTempo(bpm)
    return {"bpm": mixer.getCurrentTempo()}


_WINDOWS = {"mixer": 0, "channel_rack": 1, "playlist": 2,
            "piano_roll": 3, "browser": 4, "plugin_picker": 8}


def _cmd_ui_show(req):
    name = req.get("window", "")
    if name not in _WINDOWS:
        raise ValueError("window must be one of %s" % sorted(_WINDOWS))
    ui.showWindow(_WINDOWS[name])
    return {"window": name}


def _cmd_channels_select(req):
    # Global index space (matches channels.list and every other channel cmd).
    # value=1 -> EXCLUSIVE select so multi-select never accumulates across
    # batch imports; readback proves which channel is actually selected.
    i = int(req["channel"])
    v = int(req.get("value", 1))
    if v == 1:
        channels.selectOneChannel(i, True)
    else:
        channels.selectChannel(i, v, True)
    return {"channel": i, "value": v,
            "name": channels.getChannelName(i, True),
            "selected": bool(channels.isChannelSelected(i, True))}


def _cmd_channels_is_selected(req):
    i = int(req["channel"])
    return {"channel": i, "selected": bool(channels.isChannelSelected(i, True))}


def _cmd_ui_edit(req):
    op = req.get("op", "")
    fn = {"paste": ui.paste, "copy": ui.copy, "cut": ui.cut,
          "delete": ui.delete, "enter": ui.enter, "escape": ui.escape}.get(op)
    if fn is None:
        raise ValueError("op must be paste/copy/cut/delete/enter/escape")
    return {"op": op, "handled": fn()}


def _cmd_ui_focus(req):
    if "wid" in req:
        ui.setFocused(int(req["wid"]))
    return {"form_id": ui.getFocusedFormID(), "caption": ui.getFocusedFormCaption()}


def _cmd_undo_point(req):
    general.saveUndo(str(req.get("name", "Awknode Console")), 0)
    return {"saved": True}


def _cmd_undo_undo(req):
    general.undo()
    return {"undone": True}


def _cmd_parse_error(req):
    raise ValueError("malformed JSON: %s" % req.get("error", "unknown"))


# ---------- v2 additions (Claude Code, 2026-11-17) ----------
# Additive only: every command above keeps its exact behavior.

class _Deferred(object):
    """A command that finishes over several idle ticks (e.g. meters.sample).
    tick() -> (done, result). Reply is sent when done."""

    def __init__(self, tick, timeout_s):
        self.tick = tick
        self.deadline = time.time() + timeout_s


_pending = []  # list of (job, req_id, sink) ; sink = ("file",) or ("udp", addr)
MAX_SAMPLE_MS = 10000


def _resolve_tracks(req):
    """'tracks': list of indices and/or names; default = every named track."""
    want = req.get("tracks")
    n = mixer.trackCount()
    if want is None:
        return [i for i in range(n)
                if mixer.getTrackName(i).strip() and not mixer.getTrackName(i).startswith("Insert ")]
    out = []
    for w in want:
        out.append(_track_index({"track": w} if isinstance(w, int) else {"name": w}))
    return out


def _peak_db(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0.0:
        return "-inf"
    return round(20.0 * math.log10(v), 2)


def _cmd_mixer_meters(req):
    out = []
    for i in _resolve_tracks(req):
        pk = mixer.getTrackPeaks(i, 2)
        out.append({"index": i, "name": mixer.getTrackName(i), "peak": pk, "peak_db": _peak_db(pk)})
    return out


def _cmd_meters_sample(req):
    tracks = _resolve_tracks(req)
    ms = min(MAX_SAMPLE_MS, max(50, int(req.get("ms", 2000))))
    end = time.time() + ms / 1000.0
    acc = {i: [0.0, 0.0, 0] for i in tracks}  # max, sum, count

    def tick():
        for i in tracks:
            v = float(mixer.getTrackPeaks(i, 2))
            a = acc[i]
            a[0] = max(a[0], v)
            a[1] += v
            a[2] += 1
        if time.time() < end:
            return False, None
        res = []
        for i in tracks:
            mx, sm, n = acc[i]
            res.append({"index": i, "name": mixer.getTrackName(i), "samples": n,
                        "max": round(mx, 6), "max_db": _peak_db(mx),
                        "avg": round(sm / n, 6) if n else 0.0,
                        "avg_db": _peak_db(sm / n) if n else "-inf"})
        return True, {"ms": ms, "playing": bool(transport.isPlaying()), "tracks": res}

    return _Deferred(tick, ms / 1000.0 + 5.0)


def _slot_event_id(track, slot):
    fn = getattr(mixer, "getTrackPluginId", None)
    if not callable(fn) or midi is None or not hasattr(midi, "REC_Plug_Mute"):
        raise RuntimeError("fx bypass not supported by this FL API version")
    return fn(int(track), int(slot)) + midi.REC_Plug_Mute


def _slot_raw(track, slot):
    """Raw REC_GetValue for the slot's mute event. Meaning not yet calibrated
    (v2.0's bool reading was wrong on the owner's project, 2026-11-17)."""
    try:
        ev = _slot_event_id(track, slot)
        return general.processRECEvent(ev, -1, midi.REC_GetValue)
    except Exception as exc:
        return "err: %s" % exc


def _slot_enabled(track, slot):
    return None  # unknown until calibrated; see fx.raw


def _cmd_fx_list(req):
    i = _track_index(req)
    out = []
    for s in range(10):
        if plugins.isValid(i, s):
            out.append({"slot": s, "name": plugins.getPluginName(i, s)})
    return {"index": i, "name": mixer.getTrackName(i), "slots": out,
            "note": "slot on/off state is not readable through this FL API build"}


def _cmd_fx_raw(req):
    """Diagnostics: every REC value we can read for one slot."""
    i = _track_index(req)
    s = int(req["slot"])
    base = mixer.getTrackPluginId(i, s)
    out = {"index": i, "slot": s, "name": plugins.getPluginName(i, s) if plugins.isValid(i, s) else None,
           "plugin_id": base}
    for nm in ("REC_Plug_Mute", "REC_Plug_MixLevel", "REC_Plug_General_First"):
        off = getattr(midi, nm, None)
        if off is None:
            continue
        for flag_name in ("REC_GetValue",):
            try:
                out[nm] = general.processRECEvent(base + off, -1, getattr(midi, flag_name))
            except Exception as exc:
                out[nm] = "err: %s" % exc
    for fn_name in ("isTrackSlotsEnabled", "isTrackPluginValid"):
        fn = getattr(mixer, fn_name, None)
        if callable(fn):
            try:
                out[fn_name] = fn(i) if fn_name == "isTrackSlotsEnabled" else fn(i, s)
            except Exception as exc:
                out[fn_name] = "err: %s" % exc
    return out


def _cmd_fx_bypass(req):
    """DISABLED (v2.2, 2026-11-17): mixer.getTrackPluginId(t, s) + midi.REC_Plug_Mute
    reads the same value as REC_Plug_General_First on this FL build, i.e. it
    addresses the plugin's first parameter, not the slot bypass. Writing it would
    change a knob inside the plugin. Do not re-enable without a verified API."""
    raise RuntimeError("fx.bypass disabled: no verified slot-bypass API on this FL build")
    _require_editable()
    i = _track_index(req)
    s = int(req["slot"])
    if not plugins.isValid(i, s):
        raise ValueError("no plugin in track %d slot %d" % (i, s))
    ev = _slot_event_id(i, s)
    if "value" not in req or int(req["value"]) not in (0, 1):
        raise ValueError("value must be 1 (bypass) or 0 (enable); toggle disabled until calibrated")
    v = int(req["value"])
    before = _slot_raw(i, s)
    general.processRECEvent(ev, v, midi.REC_UpdateValue | midi.REC_UpdateControl)
    return {"index": i, "slot": s, "name": plugins.getPluginName(i, s), "sent": v,
            "raw_before": before, "raw_after": _slot_raw(i, s)}


def _plugin_target(req):
    if "track" in req:
        return _track_index(req), int(req.get("slot", 0))
    return int(req["index"]), int(req.get("slot", -1))


def _find_params(idx, slot, query, limit=25):
    q = str(query).strip().lower()
    if not q:
        raise ValueError("need 'query'")
    out = []
    for p in range(plugins.getParamCount(idx, slot)):
        name = plugins.getParamName(p, idx, slot)
        if name and q in name.lower():
            out.append({"param": p, "name": name, "value": plugins.getParamValue(p, idx, slot),
                        "value_str": _param_value_string(p, idx, slot)})
            if len(out) >= limit:
                break
    return out


def _cmd_plugins_find_param(req):
    idx, slot = _plugin_target(req)
    return {"index": idx, "slot": slot,
            "matches": _find_params(idx, slot, req.get("query", ""), int(req.get("limit", 25)))}


def _cmd_plugins_set_param_by_name(req):
    _require_editable()
    idx, slot = _plugin_target(req)
    m = _find_params(idx, slot, req.get("query", ""), 50)
    exact = [x for x in m if x["name"].strip().lower() == str(req["query"]).strip().lower()]
    pick = exact or m
    if len(pick) != 1:
        raise ValueError("query %r matched %d params: %s"
                         % (req["query"], len(pick), [x["name"] for x in pick[:10]]))
    p = pick[0]["param"]
    v = min(1.0, max(0.0, float(req["value"])))
    plugins.setParamValue(v, p, idx, slot, 0)
    return {"param": p, "name": pick[0]["name"], "value": plugins.getParamValue(p, idx, slot),
            "value_str": _param_value_string(p, idx, slot)}


def _cmd_mixer_routes(req):
    i = _track_index(req)
    fn = getattr(mixer, "getRouteSendActive", None)
    if not callable(fn):
        raise RuntimeError("routing not supported by this FL API version")
    dests = [{"index": d, "name": mixer.getTrackName(d)}
             for d in range(mixer.trackCount()) if d != i and fn(i, d)]
    return {"index": i, "name": mixer.getTrackName(i), "sends_to": dests}


def _cmd_mixer_set_route(req):
    _require_editable()
    src = _track_index({"track": req["src"]} if isinstance(req["src"], int) else {"name": req["src"]})
    dst = _track_index({"track": req["dst"]} if isinstance(req["dst"], int) else {"name": req["dst"]})
    on = 1 if int(req.get("value", 1)) else 0
    mixer.setRouteTo(src, dst, on)
    after = getattr(mixer, "afterRoutingChanged", None)
    if callable(after):
        after()
    out = {"src": src, "dst": dst, "active": bool(mixer.getRouteSendActive(src, dst))}
    if on and "level" in req:  # send level 0..1; 0 = sidechain only (no audio sent)
        setlvl = getattr(mixer, "setRouteToLevel", None)
        if not callable(setlvl):
            raise RuntimeError("route level not supported by this FL API version")
        setlvl(src, dst, min(1.0, max(0.0, float(req["level"]))))
    getlvl = getattr(mixer, "getRouteToLevel", None)
    if callable(getlvl) and out["active"]:
        out["level"] = getlvl(src, dst)
    return out


def _cmd_transport_position(req):
    """Get, or set with 'seconds' or 'bars_norm' (0..1 through the song)."""
    if "seconds" in req:
        transport.setSongPos(float(req["seconds"]), 4)  # SONGLENGTH_S
    elif "norm" in req:
        transport.setSongPos(min(1.0, max(0.0, float(req["norm"]))))
    out = {"norm": transport.getSongPos()}
    for key, mode in (("ms", 3), ("seconds", 4), ("bars", 6)):
        try:
            out[key] = transport.getSongPos(mode)
        except Exception:
            pass
    return out


def _cmd_transport_loop(req):
    """Get or toggle Pattern/Song loop mode. 'toggle': true switches it."""
    if req.get("toggle"):
        transport.setLoopMode()
    mode = transport.getLoopMode()
    return {"mode": "song" if mode else "pattern"}


def _cmd_batch(req):
    cmds = req.get("cmds")
    if not isinstance(cmds, list) or not cmds:
        raise ValueError("need non-empty 'cmds' list")
    if len(cmds) > 64:
        raise ValueError("max 64 commands per batch")
    out = []
    for c in cmds:
        if not isinstance(c, dict):
            out.append({"ok": False, "error": "command must be an object"})
            continue
        if c.get("cmd") in ("batch", "meters.sample"):
            out.append({"cmd": c.get("cmd"), "ok": False, "error": "not allowed inside batch"})
            continue
        try:
            out.append({"cmd": c.get("cmd"), "ok": True, "result": _execute(c)})
        except Exception as exc:
            out.append({"cmd": c.get("cmd"), "ok": False, "error": str(exc)})
    return out


# ---- project tidy primitives (v0.1.19): names, colours, channel -> mixer routing ----------
# Thin on purpose: judgement (families, palettes, plans) lives in the connector.

def _try(mod, fn, *args):
    f = getattr(mod, fn, None)
    if not callable(f):
        return None
    try:
        return f(*args)
    except Exception:
        return None


def _ch_try(fn, i, *rest):
    """Channel API call by GLOBAL index (what channels.list reports); falls back for old builds."""
    f = getattr(channels, fn, None)
    if not callable(f):
        return None
    try:
        return f(i, *(rest + (True,)))
    except TypeError:
        try:
            return f(i, *rest)
        except Exception:
            return None
    except Exception:
        return None


def _color_hex(c):
    if c is None:
        return None
    c = int(c) & 0xFFFFFF
    return "#%06X" % c


def _color_in(req):
    """'#RRGGBB' | 'RRGGBB' -> FL colour int (via utils.RGBToColor when present); raw:int passes as-is."""
    if "raw" in req:
        return int(req["raw"])
    spec = str(req.get("color", "")).strip().lstrip("#")
    if len(spec) != 6:
        raise ValueError("color must be '#RRGGBB' (or raw:int)")
    r, g, b = int(spec[0:2], 16), int(spec[2:4], 16), int(spec[4:6], 16)
    try:
        import utils as u
    except ImportError:
        u = None
    fn = getattr(u, "RGBToColor", None) if u is not None else None
    if callable(fn):
        try:
            return int(fn(r, g, b))
        except Exception:
            pass
    return (r << 16) | (g << 8) | b


def _cmd_mixer_set_name(req):
    _require_editable()
    i = _track_index(req)
    before = mixer.getTrackName(i)
    mixer.setTrackName(i, str(req["new_name"]))
    return {"index": i, "before": before, "name": mixer.getTrackName(i)}


def _cmd_mixer_set_color(req):
    _require_editable()
    i = _track_index(req)
    before = _try(mixer, "getTrackColor", i)
    mixer.setTrackColor(i, _color_in(req))
    after = _try(mixer, "getTrackColor", i)
    return {"index": i, "before_raw": before, "before": _color_hex(before), "color": _color_hex(after)}


def _cmd_channels_set_name(req):
    _require_editable()
    i = _channel_index(req)
    before = channels.getChannelName(i, True)
    if _ch_try("setChannelName", i, str(req["new_name"])) is None and \
            channels.getChannelName(i, True) == before and before != str(req["new_name"]):
        raise RuntimeError("setChannelName not supported by this FL API version")
    return {"index": i, "before": before, "name": channels.getChannelName(i, True)}


def _cmd_channels_set_color(req):
    _require_editable()
    i = _channel_index(req)
    before = _ch_try("getChannelColor", i)
    _ch_try("setChannelColor", i, _color_in(req))
    after = _ch_try("getChannelColor", i)
    return {"index": i, "before_raw": before, "before": _color_hex(before), "color": _color_hex(after)}


def _cmd_channels_set_target(req):
    _require_editable()
    i = _channel_index(req)
    t = int(req["track"])
    if not (0 <= t < mixer.trackCount()):
        raise ValueError("mixer track %d out of range" % t)
    before = _ch_try("getTargetFxTrack", i)
    _ch_try("setTargetFxTrack", i, t)
    return {"index": i, "before": before, "target": _ch_try("getTargetFxTrack", i)}


def _cmd_mixer_free_track(req):
    t = _try(mixer, "getFreeTrack")
    return {"track": t}


_HELP_TIDY = {
    "mixer.set_name": "track|name, new_name",
    "mixer.set_color": "track|name, color '#RRGGBB' (or raw:int from before_raw to restore)",
    "channels.set_name": "channel|name, new_name",
    "channels.set_color": "channel|name, color '#RRGGBB' (or raw:int)",
    "channels.set_target": "channel|name, track -> route the channel to that mixer track",
    "mixer.free_track": "next empty mixer track (FL's own pick)",
}


_HELP = {
    "batch": "cmds:[{cmd,...},...] (max 64; no meters.sample inside)",
    "mixer.meters": "tracks?:[index|name] -> instant peaks",
    "meters.sample": "tracks?:[index|name], ms:2000 -> max/avg peak per track over time (deferred reply)",
    "fx.list": "track|name -> slots (on/off state not readable)",
    "fx.bypass": "DISABLED - would edit the plugin's first parameter, not bypass",
    "fx.raw": "track|name, slot -> raw REC values (diagnostics)",
    "plugins.find_param": "track+slot | index+slot, query, limit?",
    "plugins.set_param_by_name": "track+slot | index+slot, query, value 0..1",
    "mixer.routes": "track|name -> tracks it sends to",
    "mixer.set_route": "src, dst (index|name), value 1/0, level? 0..1 (0 = sidechain only, no audio)",
    "transport.position": "get; or seconds:/norm: to jump",
    "transport.loop": "get; toggle:true to switch pattern/song",
    "undo.agent": "put back the agents' last group of changes (what the safety pad does)",
    "undo.log": "agent change groups the safety pad can put back",
    "mixer.set_volume": "track|name + value (unit norm|db) OR delta_db (e.g. -2 = 2 dB quieter)",
    "channels.set_volume": "channel|name + value 0..1, or unit:'db' + value, or delta_db (-2 = 2 dB quieter)",
    "help": "this list",
}


def _cmd_help(req):
    return {"commands": sorted(k for k in _COMMANDS if not k.startswith("__")), "v2": _HELP}


def _send_reply(reply, sink):
    if sink[0] == "udp":
        try:
            if _sock is not None:
                _sock.sendto(_reply_bytes(reply), sink[1])
        except OSError:
            pass
    else:
        try:
            _atomic_write_json(os.path.join(FILE_DIR, REP_NAME), json.loads(_reply_bytes(reply).decode("utf-8")))
        except Exception:
            pass


def _pump_pending():
    """Advance deferred commands; send their replies when done."""
    if not _pending:
        return
    keep = []
    for job, rid, sink in _pending:
        try:
            done, result = job.tick()
            if done:
                _send_reply({"id": rid, "ok": True, "result": result}, sink)
            elif time.time() > job.deadline:
                _send_reply({"id": rid, "ok": False, "error": "deferred-timeout"}, sink)
            else:
                keep.append((job, rid, sink))
        except Exception as exc:
            _send_reply({"id": rid, "ok": False, "error": str(exc)}, sink)
    _pending[:] = keep




# ======================================================================
# ---------- Awknode Console: hardware controller layer ----------
# ======================================================================
# A profile (io/profiles/<name>.json) maps the knobs, faders and buttons of
# whatever MIDI device this script is bound to (KeyLab 61, the MIDI port on
# an audio interface, anything) onto FL targets. Agents read and change it
# through the same JSON commands as everything else (hw.*), so Claude,
# Hermes, Codex or a plain script can all see and remap the hardware.
#
# Only CCs that a profile maps are consumed (event.handled = True). Notes,
# pitch bend, aftertouch and unmapped CCs always pass through to FL.

BUILD = "awknode-console-0.1.19"
_PASS = object()  # _hw_perform result: leave this message to FL / the host script
PROFILES_DIR = os.path.join(FILE_DIR, "profiles")
ACTIVE_NAME = "active_profile.txt"
MAX_EVENTS = 128
MAX_SEEN = 32

TARGET_KINDS = ("focused_param", "plugin_param", "mixer_volume", "mixer_pan", "channel_volume", "channel_pan",
                "selected_volume", "selected_pan", "transport", "bank", "agent", "none")
TRANSPORT_ACTIONS = ("play", "stop", "record", "loop", "toggle_play")
CC_MODES = ("abs", "rel64", "rel2c", "relsm", "button")

_hw = {
    "profile": None,      # dict
    "name": None,
    "by_cc": {},          # (chan, cc) -> control dict
    "bank": 0,
    "seen": [],           # recent raw CCs: {chan, cc, value, ts, mapped}
    "events": [],         # agent-kind presses for agents to collect
    "learn": None,        # {"ids": [...], "pos": 0, "taken": set()}
    "loaded": False,
    "error": None,
}


def _hw_validate(profile):
    """Refuse-closed validation: a bad profile never half-loads."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    controls = profile.get("controls")
    if not isinstance(controls, list):
        raise ValueError("profile needs a 'controls' list")
    ids = set()
    keys = set()
    for c in controls:
        if not isinstance(c, dict):
            raise ValueError("each control must be an object")
        cid = str(c.get("id", "")).strip()
        if not cid or cid in ids:
            raise ValueError("control id missing or duplicated: %r" % cid)
        ids.add(cid)
        mode = c.get("mode", "abs")
        if mode not in CC_MODES:
            raise ValueError("%s: mode must be one of %s" % (cid, ", ".join(CC_MODES)))
        pb = c.get("pitchbend")
        if pb is not None:
            if not isinstance(pb, int) or not 0 <= pb <= 15:
                raise ValueError("%s: pitchbend must be a MIDI channel 0..15" % cid)
            if ("pb", pb) in keys:
                raise ValueError("%s: pitch bend channel %d is already used" % (cid, pb))
            keys.add(("pb", pb))
        note = c.get("note")
        if note is not None:
            if not isinstance(note, int) or not 0 <= note <= 127:
                raise ValueError("%s: note must be 0..127" % cid)
            nch = c.get("channel", 0)
            if not isinstance(nch, int) or not 0 <= nch <= 15:
                raise ValueError("%s: channel must be 0..15" % cid)
            if ("note", nch, note) in keys:
                raise ValueError("%s: channel %d note %d is already used" % (cid, nch, note))
            keys.add(("note", nch, note))
            if (c.get("target") or {}).get("kind") != "agent":
                raise ValueError("%s: note (pad) controls can only target kind 'agent'" % cid)
            if (c.get("target") or {}).get("tap") not in (None, "undo"):
                raise ValueError("%s: agent 'tap' can only be 'undo'" % cid)
        cc = c.get("cc")
        if cc is not None:
            if not isinstance(cc, int) or not 0 <= cc <= 127:
                raise ValueError("%s: cc must be 0..127 or null" % cid)
            ch = c.get("channel", 0)
            if not isinstance(ch, int) or not 0 <= ch <= 15:
                raise ValueError("%s: channel must be 0..15" % cid)
            if (ch, cc) in keys:
                raise ValueError("%s: channel %d cc %d is already used" % (cid, ch, cc))
            keys.add((ch, cc))
        t = c.get("target") or {"kind": "none"}
        if not isinstance(t, dict) or t.get("kind") not in TARGET_KINDS:
            raise ValueError("%s: target.kind must be one of %s" % (cid, ", ".join(TARGET_KINDS)))
        k = t["kind"]
        if k in ("focused_param",) and not isinstance(t.get("param"), int):
            raise ValueError("%s: focused_param needs integer 'param'" % cid)
        if k == "plugin_param":
            for f in ("index", "param"):
                if not isinstance(t.get(f), int):
                    raise ValueError("%s: plugin_param needs integer '%s'" % (cid, f))
        if k in ("mixer_volume", "mixer_pan", "channel_volume", "channel_pan") and not isinstance(t.get("offset"), int):
            raise ValueError("%s: %s needs integer 'offset' (0 = first track of the bank)" % (cid, k))
        if k == "transport" and t.get("action") not in TRANSPORT_ACTIONS:
            raise ValueError("%s: transport action must be one of %s" % (cid, ", ".join(TRANSPORT_ACTIONS)))
        if k == "bank" and t.get("step") not in (-1, 1):
            raise ValueError("%s: bank step must be -1 or 1" % cid)
    maps = profile.get("plugin_maps", {})
    if not isinstance(maps, dict) or not all(
            v == "midi" or (isinstance(v, list) and all(isinstance(n, str) for n in v)) for v in maps.values()):
        raise ValueError('plugin_maps must be {plugin name: [parameter names] or "midi"}')
    return profile


_param_cache = {}  # (plugin name, index, slot, param name) -> param index or -1
PARAM_SCAN_LIMIT = 1024  # Pro-Q 4 keeps Output Level at 556
PASS_MIDI = -2  # _hw_mapped_param: send the raw CC on to the plugin instead
# Plugins whose layout names must exist: never fall back to a parameter index
# (MIDI Out's index 0 is its General MIDI patch selector).
STRICT_PLUGINS = ("midi out", "fruity midi out")


def _hw_mapped_param(idx, slot, pos):
    """Knob position -> parameter index, via the profile's per-plugin name layout."""
    maps = (_hw["profile"] or {}).get("plugin_maps") or {}
    try:
        pname = plugins.getPluginName(idx, slot)
    except Exception:
        try:
            pname = ui.getFocusedPluginName()
        except Exception:
            pname = None
    names = maps.get(pname)
    if names is None and pname:
        low = str(pname).strip().lower()
        names = next((v for k, v in maps.items() if k.strip().lower() == low), None)
    if names == "midi":
        return PASS_MIDI  # the plugin learns the CCs itself (Kontakt, Omnisphere, MIDI Out)
    if not names:
        return pos
    if pos >= len(names) or not names[pos]:
        return -1  # this plugin's layout leaves the control empty
    want = names[pos].strip().lower()
    key = (pname, idx, slot, want)
    hit = _param_cache.get(key)
    if hit is None:
        hit = -1
        for p in range(min(plugins.getParamCount(idx, slot), PARAM_SCAN_LIMIT)):
            if str(plugins.getParamName(p, idx, slot)).strip().lower() == want:
                hit = p
                break
        if hit >= 0 or not (want.startswith("awk ") or str(pname).strip().lower() in STRICT_PLUGINS):
            _param_cache[key] = hit  # an Awknode preset may be loaded later: keep looking
    strict = want.startswith("awk ") or str(pname).strip().lower() in STRICT_PLUGINS
    if hit < 0 and strict:
        return -1  # Awknode-labelled controls (MIDI Out presets): never fall back to an index
    return hit if hit >= 0 else pos


def _hw_index(profile):
    by_cc = {}
    for c in profile.get("controls", []):
        if c.get("cc") is not None:
            by_cc[(int(c.get("channel", 0)), int(c["cc"]))] = c
    return by_cc


def _hw_pb_index(profile):
    return {int(c["pitchbend"]): c for c in profile.get("controls", []) if c.get("pitchbend") is not None}


def _hw_apply_profile(name, profile):
    _hw_validate(profile)
    _hw["profile"] = profile
    _hw["name"] = name
    _hw["by_cc"] = _hw_index(profile)
    _hw["by_pb"] = _hw_pb_index(profile)
    _hw["bank"] = 0
    _hw["error"] = None


def _hw_profile_path(name):
    name = str(name).strip()
    if not name or any(ch in name for ch in '\\/:*?"<>|.') or len(name) > 64:
        raise ValueError("bad profile name %r (letters, digits, - and _ only)" % name)
    return os.path.join(PROFILES_DIR, name + ".json")


def _hw_list_profiles():
    try:
        return sorted(f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith(".json"))
    except Exception:
        return []


def _hw_load_active():
    """Called once the file transport is up (FL refuses IO in OnInit)."""
    _hw["loaded"] = True
    try:
        name = "keylab61"
        p = os.path.join(FILE_DIR, ACTIVE_NAME)
        if _file_exists(p):
            name = _file_read(p).strip() or name
        _hw_apply_profile(name, json.loads(_file_read(_hw_profile_path(name))))
        print("Awknode Console: profile '%s' (%d mapped)" % (name, len(_hw["by_cc"])))
    except Exception as exc:
        _hw["error"] = "profile load failed: %s" % exc
        print("Awknode Console: " + _hw["error"])


def _hw_save(name, profile):
    _hw_validate(profile)
    _file_write(_hw_profile_path(name), json.dumps(profile, indent=2))


def _hw_value(control, data2):
    """CC byte -> (kind, value): ('abs', 0..1) or ('rel', delta) or ('press', bool)."""
    mode = control.get("mode", "abs")
    if mode == "button":
        return "press", data2 >= 64
    if mode == "rel64":
        return "rel", (data2 - 64) / 127.0 * float(control.get("speed", 1.0))
    if mode == "relsm":  # sign-magnitude: 1..63 = up, 65..127 = down (KeyLab DAW mode)
        d = data2 if data2 < 64 else -(data2 - 64)
        return "rel", d / 127.0 * float(control.get("speed", 1.0))
    if mode == "rel2c":
        d = data2 - 128 if data2 >= 64 else data2
        return "rel", d / 127.0 * float(control.get("speed", 1.0))
    lo, hi = control.get("range", [0.0, 1.0])
    v = data2 / 127.0
    if control.get("invert"):
        v = 1.0 - v
    return "abs", lo + (hi - lo) * v


def _hw_plugin_window_focused():
    try:
        return bool(ui.getFocused(5) or ui.getFocused(6) or ui.getFocused(7))
    except Exception:
        return False


_last_plugin = [None]  # sticky target: (index, slot, plugin name) of the last focused plugin


def _hw_plugin_name(idx, slot):
    try:
        return plugins.getPluginName(idx, slot)
    except Exception:
        return None


def _hw_window_plugin():
    """(index, slot) of the plugin window that has focus right now, or None."""
    try:
        if ui.getFocused(6):  # widPluginEffect
            fid = ui.getFocusedFormID()
            return (fid >> 22) & 0x7F, (fid >> 16) & 0x3F
        if ui.getFocused(7) or ui.getFocused(5):  # widPluginGenerator / widPlugin
            return channels.selectedChannel(), -1
    except Exception:
        pass
    return None


def _hw_track_focus():
    """Remember the last plugin window that had focus (called every idle tick)."""
    fp = _hw_window_plugin()
    if fp is not None:
        _last_plugin[0] = (fp[0], fp[1], _hw_plugin_name(fp[0], fp[1]))


def _hw_sticky_plugin():
    lp = _last_plugin[0]
    if lp is None:
        return None
    try:
        if plugins.isValid(lp[0], lp[1]) and _hw_plugin_name(lp[0], lp[1]) == lp[2]:
            return lp[0], lp[1]
    except Exception:
        pass
    _last_plugin[0] = None  # removed or replaced: forget it
    return None


def _hw_focused_plugin():
    """Focused plugin window, else the last focused plugin, else the selected channel."""
    fp = _hw_window_plugin()
    if fp is not None:
        _last_plugin[0] = (fp[0], fp[1], _hw_plugin_name(fp[0], fp[1]))
        return fp
    fp = _hw_sticky_plugin()
    if fp is not None:
        return fp
    try:
        ch = channels.selectedChannel()
        if ch >= 0 and plugins.isValid(ch, -1):
            return ch, -1
    except Exception:
        pass
    return None


def _hw_set_param(idx, slot, param, kind, value):
    if not plugins.isValid(idx, slot):
        return None
    if param >= plugins.getParamCount(idx, slot):
        return None
    if kind == "rel":
        value = plugins.getParamValue(param, idx, slot) + value
    value = min(1.0, max(0.0, value))
    try:
        plugins.setParamValue(value, param, idx, slot, 2 if kind == "abs" else 0)  # 2 = FL's pickup setting
    except TypeError:
        plugins.setParamValue(value, param, idx, slot)
    return value


def _hw_bank_track(offset):
    t = 1 + _hw["bank"] * int((_hw["profile"] or {}).get("bank_size", 8)) + offset
    return t if 0 < t < mixer.trackCount() else None


def _hw_mixer(track, what, kind, value):
    if track is None:
        return None
    if what == "volume":
        if kind == "rel":
            value = mixer.getTrackVolume(track) + value
        value = min(1.0, max(0.0, value))
        try:
            mixer.setTrackVolume(track, value, 2 if kind == "abs" else 0)
        except TypeError:
            mixer.setTrackVolume(track, value)
    else:
        if kind == "abs":
            value = value * 2.0 - 1.0
        else:
            value = mixer.getTrackPan(track) + value * 2.0
        value = min(1.0, max(-1.0, value))
        try:
            mixer.setTrackPan(track, value, 2 if kind == "abs" else 0)
        except TypeError:
            mixer.setTrackPan(track, value)
    return value


def _hw_perform(control, data2):
    """Apply one mapped CC. Returns a short description (for the seen log)."""
    t = control.get("target") or {"kind": "none"}
    k = t["kind"]
    kind, v = _hw_value(control, data2)
    if kind == "press":
        if not v:
            return "release"
        if k == "transport":
            a = t["action"]
            if a == "play":
                transport.start()
            elif a == "toggle_play":
                transport.start()  # FL's start() toggles play/pause
            elif a == "stop":
                transport.stop()
            elif a == "record":
                transport.record()
            elif a == "loop":
                transport.setLoopMode()
            return "transport " + a
        if k == "bank":
            _hw["bank"] = max(0, _hw["bank"] + t["step"])
            return "bank %d" % _hw["bank"]
        if k == "agent":
            ev = {"id": control["id"], "name": t.get("name", control["id"]), "ts": round(time.time(), 3)}
            _hw["events"].append(ev)
            del _hw["events"][:-MAX_EVENTS]
            return "agent event " + ev["name"]
        return "press (no action)"
    if k == "focused_param":
        if t.get("require_focus") and not _hw_plugin_window_focused() and _hw_sticky_plugin() is None:
            return _PASS  # no plugin focused yet: don't touch a hidden channel
        fp = _hw_focused_plugin()
        if fp is None:
            return "no plugin focused"
        param = _hw_mapped_param(fp[0], fp[1], t["param"])
        if param == PASS_MIDI:
            return _PASS  # FL sends the CC to the focused channel; its MIDI Learn takes it
        if param < 0:
            return "empty in this plugin's layout"
        r = _hw_set_param(fp[0], fp[1], param, kind, v)
        return "focused %s.%s p%d=%s" % (fp[0], fp[1], param, r)
    if k == "plugin_param":
        r = _hw_set_param(t["index"], t.get("slot", -1), t["param"], kind, v)
        return "plugin p%d=%s" % (t["param"], r)
    if k in ("mixer_volume", "mixer_pan"):
        tr = _hw_bank_track(t["offset"])
        return "track %s %s=%s" % (tr, k[6:], _hw_mixer(tr, k[6:], kind, v))
    if k in ("channel_volume", "channel_pan"):
        ch = _hw["bank"] * int((_hw["profile"] or {}).get("bank_size", 8)) + t["offset"]
        if not 0 <= ch < channels.channelCount():
            return "no channel %d" % ch
        what = k[8:]
        if what == "volume":
            cur = channels.getChannelVolume(ch)
            val = min(1.0, max(0.0, cur + v if kind == "rel" else v))
            try:
                channels.setChannelVolume(ch, val, 2 if kind == "abs" else 0)
            except TypeError:
                channels.setChannelVolume(ch, val)
        else:
            cur = channels.getChannelPan(ch)
            val = min(1.0, max(-1.0, cur + v * 2.0 if kind == "rel" else v * 2.0 - 1.0))
            try:
                channels.setChannelPan(ch, val, 2 if kind == "abs" else 0)
            except TypeError:
                channels.setChannelPan(ch, val)
        return "channel %d %s=%s" % (ch, what, round(val, 4))
    if k in ("selected_volume", "selected_pan"):
        tr = mixer.trackNumber()
        return "track %s %s=%s" % (tr, k[9:], _hw_mixer(tr, k[9:], kind, v))
    if k == "agent":
        ev = {"id": control["id"], "name": t.get("name", control["id"]),
              "value": round(v, 4), "ts": round(time.time(), 3)}
        _hw["events"].append(ev)
        del _hw["events"][:-MAX_EVENTS]
        return "agent value"
    return "unmapped target"


def _hw_learn_take(chan, cc):
    """Bind the next control in a learn run to this CC. Returns True if taken."""
    L = _hw["learn"]
    if L is None or (chan, cc) in L["taken"]:
        return L is not None  # repeats of the same knob are swallowed while learning
    prof = _hw["profile"]
    cid = L["ids"][L["pos"]]
    for c in prof["controls"]:
        if c.get("cc") == cc and int(c.get("channel", 0)) == chan:
            c["cc"] = None  # steal it from whatever had it
    for c in prof["controls"]:
        if c["id"] == cid:
            c["cc"] = cc
            c["channel"] = chan
    L["taken"].add((chan, cc))
    L["bound"].append({"id": cid, "channel": chan, "cc": cc})
    L["pos"] += 1
    _hw["by_cc"] = _hw_index(prof)
    if L["pos"] >= len(L["ids"]):
        L["done"] = True
        _hw["learn"] = None
        _hw["last_learn"] = L
        if L.get("save", True) and _hw["name"]:
            try:
                _hw_save(_hw["name"], prof)
            except Exception as exc:
                _hw["error"] = "learn save failed: %s" % exc
    return True


def _hw_on_cc(event):
    chan = event.midiChan
    cc = event.data1
    rec = {"channel": chan, "cc": cc, "value": event.data2, "ts": round(time.time(), 3)}
    _hw["seen"].append(rec)
    del _hw["seen"][:-MAX_SEEN]
    if _hw["profile"] is None:
        return False
    if _hw["learn"] is not None:
        if _hw_learn_take(chan, cc):
            rec["learn"] = True
            return True
    control = _hw["by_cc"].get((chan, cc))
    if control is None:
        return False
    try:
        did = _hw_perform(control, event.data2)
        rec["control"] = control["id"]
        if did is _PASS:
            rec["did"] = "pass"
            return False
        rec["did"] = did
    except Exception as exc:
        rec["error"] = str(exc)
    return True


def _hw_on_pitchbend(event):
    """Faders that send pitch bend (KeyLab DAW mode: fader N = channel N-1)."""
    chan = event.status & 0x0F
    control = _hw.get("by_pb", {}).get(chan)
    rec = {"status": "0xE0", "channel": chan, "data1": event.data1, "data2": event.data2,
           "ts": round(time.time(), 3)}
    _hw["seen"].append(rec)
    del _hw["seen"][:-MAX_SEEN]
    if control is None or _hw["learn"] is not None:
        return False
    try:
        value7 = int(round(((event.data2 << 7) | event.data1) / 16383.0 * 127))
        did = _hw_perform(control, value7)
        rec["control"] = control["id"]
        if did is _PASS:
            rec["did"] = "pass"
            return False
        rec["did"] = did
    except Exception as exc:
        rec["error"] = str(exc)
    return True


# ---------- hw.* agent commands ----------

def _cmd_hw_status(req):
    prof = _hw["profile"] or {}
    L = _hw["learn"]
    return {
        "build": BUILD,
        "profile": _hw["name"],
        "device": prof.get("device"),
        "mapped": len(_hw["by_cc"]),
        "unmapped": [c["id"] for c in prof.get("controls", []) if c.get("cc") is None],
        "bank": _hw["bank"],
        "learning": None if L is None else {"next": L["ids"][L["pos"]], "remaining": L["ids"][L["pos"]:]},
        "last_learn": (_hw.get("last_learn") or {}).get("bound"),
        "pending_events": len(_hw["events"]),
        "error": _hw["error"],
        "profiles": _hw_list_profiles(),
    }


def _cmd_hw_profile_get(req):
    name = req.get("name")
    if name and name != _hw["name"]:
        return {"name": name, "profile": json.loads(_file_read(_hw_profile_path(name)))}
    return {"name": _hw["name"], "profile": _hw["profile"]}


def _cmd_hw_profile_set(req):
    """Replace (and save) a profile. activate:true makes it live."""
    name = str(req.get("name") or _hw["name"] or "").strip()
    profile = req.get("profile")
    _hw_validate(profile)
    _hw_save(name, profile)
    if req.get("activate", name == _hw["name"]):
        _hw_apply_profile(name, profile)
        _file_write(os.path.join(FILE_DIR, ACTIVE_NAME), name)
    return {"saved": name, "active": _hw["name"]}


def _cmd_hw_profile_activate(req):
    name = str(req.get("name", "")).strip()
    _hw_apply_profile(name, json.loads(_file_read(_hw_profile_path(name))))
    _file_write(os.path.join(FILE_DIR, ACTIVE_NAME), name)
    return _cmd_hw_status(req)


def _cmd_hw_map(req):
    """Change one control's target (and optionally its cc) live, then save."""
    prof = _hw["profile"]
    if prof is None:
        raise ValueError("no profile loaded")
    cid = req.get("control")  # not "id": that is the request id
    new = json.loads(json.dumps(prof))
    hit = [c for c in new["controls"] if c["id"] == cid]
    if not hit:
        raise ValueError("no control %r; ids: %s" % (cid, [c["id"] for c in new["controls"]]))
    c = hit[0]
    for f in ("target", "cc", "channel", "mode", "label", "invert", "range", "speed"):
        if f in req:
            c[f] = req[f]
    _hw_apply_profile(_hw["name"], new)
    if req.get("save", True):
        _hw_save(_hw["name"], new)
    return {"control": c}


def _cmd_hw_learn(req):
    """Start a learn run: move the hardware controls in the order of 'ids'."""
    prof = _hw["profile"]
    if prof is None:
        raise ValueError("no profile loaded")
    ids = req.get("ids")
    all_ids = [c["id"] for c in prof["controls"]]
    if ids in (None, "all"):
        ids = all_ids
    elif ids == "unmapped":
        ids = [c["id"] for c in prof["controls"] if c.get("cc") is None]
    bad = [i for i in ids if i not in all_ids]
    if bad or not ids:
        raise ValueError("unknown or empty ids: %s" % bad)
    _hw["learn"] = {"ids": list(ids), "pos": 0, "taken": set(), "bound": [],
                    "save": bool(req.get("save", True)), "done": False}
    return {"learning": True, "first": ids[0], "count": len(ids),
            "say": "Move each control once, in this order: " + ", ".join(ids)}


def _cmd_hw_learn_cancel(req):
    L = _hw["learn"]
    _hw["learn"] = None
    return {"cancelled": L is not None, "bound": (L or {}).get("bound", [])}


def _cmd_hw_seen(req):
    return {"seen": list(_hw["seen"])}


def _cmd_hw_events(req):
    out = list(_hw["events"])
    if req.get("clear", True):
        del _hw["events"][:]
    return {"events": out}


def _cmd_hw_bank(req):
    if "bank" in req:
        _hw["bank"] = max(0, int(req["bank"]))
    return {"bank": _hw["bank"]}


def _cmd_hw_simulate(req):
    """Feed a fake CC through the mapping (testing without touching hardware)."""
    class _E(object):
        pass
    e = _E()
    e.midiChan = int(req.get("channel", 0))
    e.data1 = int(req["cc"])
    e.data2 = int(req.get("value", 127))
    handled = _hw_on_cc(e)
    return {"handled": handled, "record": _hw["seen"][-1]}


_HW_HELP = {
    "hw.status": "profile, mapped/unmapped controls, bank, learn state, pending agent events",
    "hw.profile.get": "name? -> the profile JSON",
    "hw.profile.set": "name, profile, activate? -> validate + save (+ make live)",
    "hw.profile.activate": "name -> load io/profiles/<name>.json and make it live",
    "hw.map": "control, target?, cc?, channel?, mode?, label?, invert?, range?, speed?, save? -> change one control",
    "hw.learn": "ids: [..] | 'all' | 'unmapped' -> next CCs bind to these controls in order",
    "hw.learn.cancel": "stop a learn run",
    "hw.seen": "last 32 raw CCs the hardware sent (what does this knob send?)",
    "hw.events": "clear? -> button/knob events from controls mapped to kind 'agent'",
    "hw.bank": "bank? -> get/set the mixer bank the faders control",
    "hw.simulate": "cc, value, channel? -> run a CC through the mapping without hardware",
}

_COMMANDS = {
    "ping": _cmd_ping,
    "info": _cmd_info,
    "mixer.list": _cmd_mixer_list,
    "mixer.get": _cmd_mixer_get,
    "mixer.set_volume": _cmd_mixer_set_volume,
    "mixer.set_pan": _cmd_mixer_set_pan,
    "mixer.mute": _cmd_mixer_mute,
    "mixer.solo": _cmd_mixer_solo,
    "mixer.select": _cmd_mixer_select,
    "mixer.open_effect": _cmd_mixer_open_effect,
    "mixer.peaks": _cmd_mixer_peaks,
    "channels.list": _cmd_channels_list,
    "channels.set_volume": _cmd_channels_set_volume,
    "channels.mute": _cmd_channels_mute,
    "channels.solo": _cmd_channels_solo,
    "channels.open_editor": _cmd_channels_open_editor,
    "plugins.list": _cmd_plugins_list,
    "plugins.params": _cmd_plugins_params,
    "plugins.get_param": _cmd_plugins_get_param,
    "plugins.set_param": _cmd_plugins_set_param,
    "plugins.preset": _cmd_plugins_preset,
    "transport.start": _cmd_transport_start,
    "transport.stop": _cmd_transport_stop,
    "transport.status": _cmd_transport_status,
    "tempo.get": _cmd_tempo_get,
    "tempo.set": _cmd_tempo_set,
    "ui.show": _cmd_ui_show,
    "ui.edit": _cmd_ui_edit,
    "ui.focus": _cmd_ui_focus,
    "channels.select": _cmd_channels_select,
    "channels.is_selected": _cmd_channels_is_selected,
    "undo.point": _cmd_undo_point,
    "undo.undo": _cmd_undo_undo,
    "undo.agent": lambda req: _cmd_undo_agent(req),
    "undo.log": lambda req: _cmd_undo_log(req),
    "__parse_error__": _cmd_parse_error,
    # v2
    "batch": _cmd_batch,
    "help": _cmd_help,
    "mixer.meters": _cmd_mixer_meters,
    "meters.sample": _cmd_meters_sample,
    "fx.list": _cmd_fx_list,
    "fx.bypass": _cmd_fx_bypass,
    "fx.raw": _cmd_fx_raw,
    "plugins.find_param": _cmd_plugins_find_param,
    "plugins.set_param_by_name": _cmd_plugins_set_param_by_name,
    "mixer.routes": _cmd_mixer_routes,
    "mixer.set_route": _cmd_mixer_set_route,
    "transport.position": _cmd_transport_position,
    "transport.loop": _cmd_transport_loop,
}


_COMMANDS.update({
    "hw.status": _cmd_hw_status,
    "hw.profile.get": _cmd_hw_profile_get,
    "hw.profile.set": _cmd_hw_profile_set,
    "hw.profile.activate": _cmd_hw_profile_activate,
    "hw.map": _cmd_hw_map,
    "hw.learn": _cmd_hw_learn,
    "hw.learn.cancel": _cmd_hw_learn_cancel,
    "hw.seen": _cmd_hw_seen,
    "hw.events": _cmd_hw_events,
    "hw.bank": _cmd_hw_bank,
    "hw.simulate": _cmd_hw_simulate,
})


_COMMANDS.update({
    "mixer.set_name": _cmd_mixer_set_name,
    "mixer.set_color": _cmd_mixer_set_color,
    "channels.set_name": _cmd_channels_set_name,
    "channels.set_color": _cmd_channels_set_color,
    "channels.set_target": _cmd_channels_set_target,
    "mixer.free_track": _cmd_mixer_free_track,
})
_HELP.update(_HELP_TIDY)
_HELP.update(_HW_HELP)


# ---------- transport layer ----------

# ---------- agent change log (the safety pad's undo) ----------
# FL's undo history doesn't record script-made plugin/mixer changes, so the console
# keeps its own: before an agent changes a value, the old value is logged. A group is
# everything since the agent's last undo.point (or one command, without one).
JOURNAL_MAX = 50
GROUP_SECONDS = 120.0
_journal = []  # [{"name", "ts", "point": bool, "items": [(key, restore_args)], "keys": set}]


def _journal_snapshot(req):
    """(key, restore tuple) for what this command is about to change, or None."""
    cmd = req.get("cmd")
    if cmd == "mixer.set_volume":
        i = _track_index(req)
        return ("mixvol", i), ("mixvol", i, mixer.getTrackVolume(i))
    if cmd == "mixer.set_pan":
        i = _track_index(req)
        return ("mixpan", i), ("mixpan", i, mixer.getTrackPan(i))
    if cmd == "mixer.mute":
        i = _track_index(req)
        return ("mixmute", i), ("mixmute", i, int(bool(mixer.isTrackMuted(i))))
    if cmd == "channels.set_volume":
        i = _channel_index(req)
        return ("chvol", i), ("chvol", i, channels.getChannelVolume(i, 0, True))
    if cmd == "channels.mute":
        i = int(req["channel"])
        return ("chmute", i), ("chmute", i, int(bool(channels.isChannelMuted(i, True))))
    if cmd == "plugins.set_param":
        idx, slot, p = int(req["index"]), int(req.get("slot", -1)), int(req["param"])
        return ("param", idx, slot, p), ("param", idx, slot, p, plugins.getParamValue(p, idx, slot))
    if cmd == "plugins.set_param_by_name":
        idx, slot = _plugin_target(req)
        m = _find_params(idx, slot, req.get("query", ""), 50)
        exact = [x for x in m if x["name"].strip().lower() == str(req["query"]).strip().lower()]
        pick = exact or m
        if len(pick) != 1:
            return None  # the command itself will refuse
        p = pick[0]["param"]
        return ("param", idx, slot, p), ("param", idx, slot, p, plugins.getParamValue(p, idx, slot))
    if cmd == "mixer.set_route":
        src = _track_index({"track": req["src"]} if isinstance(req["src"], int) else {"name": req["src"]})
        dst = _track_index({"track": req["dst"]} if isinstance(req["dst"], int) else {"name": req["dst"]})
        getlvl = getattr(mixer, "getRouteToLevel", None)
        active = int(bool(mixer.getRouteSendActive(src, dst)))
        lvl = getlvl(src, dst) if callable(getlvl) and active else None
        return ("route", src, dst), ("route", src, dst, active, lvl)
    if cmd == "tempo.set":
        return ("tempo",), ("tempo", mixer.getCurrentTempo())
    return None


def _journal_group(name=None, point=False):
    g = {"name": name or "agent change", "ts": time.time(), "point": point, "items": [], "keys": set()}
    _journal.append(g)
    del _journal[:-JOURNAL_MAX]
    return g


def _journal_record(req):
    try:
        snap = _journal_snapshot(req)
    except Exception:
        snap = None
    if snap is None:
        return
    key, restore = snap
    g = _journal[-1] if _journal else None
    if g is None or (not g["point"] and g["items"]) or time.time() - g["ts"] > GROUP_SECONDS:
        g = _journal_group(req.get("cmd"))
    if key not in g["keys"]:  # keep the value from BEFORE the group started
        g["keys"].add(key)
        g["items"].append(restore)


def _journal_restore(item):
    k = item[0]
    if k == "mixvol":
        mixer.setTrackVolume(item[1], item[2], 0)
    elif k == "mixpan":
        mixer.setTrackPan(item[1], item[2], 0)
    elif k == "mixmute":
        if int(bool(mixer.isTrackMuted(item[1]))) != item[2]:
            mixer.muteTrack(item[1], item[2])
    elif k == "chvol":
        channels.setChannelVolume(item[1], item[2], 0, True)
    elif k == "chmute":
        if int(bool(channels.isChannelMuted(item[1], True))) != item[2]:
            channels.muteChannel(item[1], item[2], True)
    elif k == "param":
        plugins.setParamValue(item[4], item[3], item[1], item[2], 0)
    elif k == "tempo":
        mixer.setCurrentTempo(item[1])
    elif k == "route":
        mixer.setRouteTo(item[1], item[2], item[3])
        if item[3] and item[4] is not None and callable(getattr(mixer, "setRouteToLevel", None)):
            mixer.setRouteToLevel(item[1], item[2], item[4])
        if callable(getattr(mixer, "afterRoutingChanged", None)):
            mixer.afterRoutingChanged()


def _journal_undo():
    """Put back the agents' most recent group of changes. None if there is nothing."""
    while _journal and not _journal[-1]["items"]:
        _journal.pop()
    if not _journal:
        return None
    g = _journal.pop()
    errors = 0
    for item in reversed(g["items"]):
        try:
            _journal_restore(item)
        except Exception:
            errors += 1
    return {"name": g["name"], "restored": len(g["items"]) - errors, "errors": errors}


def _cmd_undo_agent(req):
    r = _journal_undo()
    return r or {"restored": 0, "note": "no agent changes logged"}


def _cmd_undo_log(req):
    return {"groups": [{"name": g["name"], "age_s": round(time.time() - g["ts"], 1),
                        "changes": len(g["items"])} for g in _journal if g["items"]]}


def _execute(req):
    cmd = req.get("cmd")
    handler = _COMMANDS.get(cmd)
    if handler is None:
        raise ValueError("unknown cmd %r; valid: %s"
                         % (cmd, ", ".join(sorted(k for k in _COMMANDS if not k.startswith("__")))))
    if cmd == "undo.point":
        _journal_group(str(req.get("name", "Awknode Console")), point=True)
    else:
        _journal_record(req)
    return handler(req)


def _build_reply(req):
    rid = req.get("id") if isinstance(req, dict) else None
    try:
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        result = _execute(req)
        if isinstance(result, _Deferred):
            return result  # caller queues it; reply is sent when it finishes
        return {"id": rid, "ok": True, "result": result}
    except Exception as exc:
        return {"id": rid, "ok": False, "error": str(exc)}


def _reply_bytes(reply):
    rid = reply.get("id")
    try:
        data = json.dumps(reply, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError):
        data = json.dumps({"id": rid, "ok": False,
                           "error": "result-not-serializable"}).encode("utf-8")
    if len(data) > MAX_REPLY_BYTES:
        data = json.dumps({"id": rid, "ok": False, "error": "response-too-large",
                           "bytes": len(data),
                           "hint": "use offset/limit"}).encode("utf-8")
    return data


def _handle(addr, req):
    reply = _build_reply(req)
    if isinstance(reply, _Deferred):
        _pending.append((reply, req.get("id"), ("udp", addr)))
        return
    data = _reply_bytes(reply)
    try:
        if _sock is not None:
            _sock.sendto(data, addr)
    except OSError:
        pass


def _wsa_startup():
    """Best-effort Winsock init — FL's embedded Python can leave winsock
    uninitialized, making _socket.socket.__init__ die with SystemError."""
    try:
        import ctypes
        buf = ctypes.create_string_buffer(408)
        ctypes.windll.ws2_32.WSAStartup(0x0202, ctypes.byref(buf))
    except Exception:
        pass


# ---------- file I/O backends ----------
# FL's embedded Python can refuse C-level OS calls (socket, mkdir) with
# SystemError. Builtin open/stat usually still work (imports prove reads do).
# If builtins fail too, ctypes calls the Windows API directly, bypassing the
# broken builtin wrappers entirely.

_k32 = None


def _ct_setup():
    global _k32
    if _k32 is not None:
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.GetFileSize.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
    k32.GetFileAttributesW.restype = wintypes.DWORD
    k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    k32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _k32 = k32


def _ct_write(path, data):
    _ct_setup()
    import ctypes
    from ctypes import wintypes
    h = _k32.CreateFileW(path, 0x40000000, 0, None, 2, 0x80, None)  # GENERIC_WRITE, CREATE_ALWAYS
    if h is None or h == ctypes.c_void_p(-1).value:
        raise OSError("CreateFileW(write) failed: %d" % _k32.GetLastError())
    try:
        written = wintypes.DWORD()
        if not _k32.WriteFile(h, data, len(data), ctypes.byref(written), None):
            raise OSError("WriteFile failed: %d" % _k32.GetLastError())
    finally:
        _k32.CloseHandle(h)


def _ct_read(path):
    _ct_setup()
    import ctypes
    from ctypes import wintypes
    h = _k32.CreateFileW(path, 0x80000000, 1, None, 3, 0x80, None)  # GENERIC_READ, SHARE_READ, OPEN_EXISTING
    if h is None or h == ctypes.c_void_p(-1).value:
        raise OSError("CreateFileW(read) failed: %d" % _k32.GetLastError())
    try:
        size = _k32.GetFileSize(h, None)
        if size == 0xFFFFFFFF:
            raise OSError("GetFileSize failed: %d" % _k32.GetLastError())
        buf = ctypes.create_string_buffer(size)
        got = wintypes.DWORD()
        if not _k32.ReadFile(h, buf, size, ctypes.byref(got), None):
            raise OSError("ReadFile failed: %d" % _k32.GetLastError())
        return buf.raw[:got.value]
    finally:
        _k32.CloseHandle(h)


def _ct_exists(path):
    _ct_setup()
    return _k32.GetFileAttributesW(path) != 0xFFFFFFFF


def _ct_delete(path):
    _ct_setup()
    if not _k32.DeleteFileW(path):
        raise OSError("DeleteFileW failed: %d" % _k32.GetLastError())


def _ct_replace(src, dst):
    _ct_setup()
    if not _k32.MoveFileExW(src, dst, 1):  # MOVEFILE_REPLACE_EXISTING
        raise OSError("MoveFileExW failed: %d" % _k32.GetLastError())


# ---------- raw-fd I/O backend ----------
# FL 2026 runs device scripts in a Python subinterpreter whose context refuses
# _socket.socket(), _io.FileIO (builtin open) and _ctypes with SystemError
# "returned NULL without setting an exception". os.open/os.read/os.write deal
# only in int fds and bytes — no cross-interpreter C-type construction — so
# they survive where the object-constructing wrappers die. No open(), no
# ctypes, no socket anywhere in this backend.

def _rf_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while len(view):
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)


def _rf_read(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _rf_exists(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    os.close(fd)
    return True


def _rf_delete(path):
    os.unlink(path)


def _rf_replace(src, dst):
    try:
        os.replace(src, dst)
    except Exception:
        try:
            os.unlink(dst)
        except Exception:
            pass
        os.rename(src, dst)


# ---------- Win32 handle I/O backend ----------
# FL 2026's device-script context rejects os.open (TypeError) and builtin
# open (_io.FileIO SystemError) while os.stat/listdir/unlink stay alive.
# _winapi.CreateFile/ReadFile/WriteFile are the raw Win32 calls underneath
# Python's own IO — they deal in int handles + bytes tuples, the same class
# of operation as os.stat, and bypass both broken wrappers. Verified
# end-to-end on FL's embedded CPython 3.12.1 (Windows).

_WH_GENERIC_READ = 0x80000000
_WH_GENERIC_WRITE = 0x40000000
_WH_FILE_SHARE_READ = 0x1
_WH_CREATE_ALWAYS = 2
_WH_OPEN_EXISTING = 3
_WH_FILE_ATTRIBUTE_NORMAL = 0x80


def _wh_write(path, data):
    h = _winapi.CreateFile(path, _WH_GENERIC_WRITE, 0, 0,
                           _WH_CREATE_ALWAYS, _WH_FILE_ATTRIBUTE_NORMAL, 0)
    try:
        written, err = _winapi.WriteFile(h, data)
        if err or written != len(data):
            raise OSError("WriteFile err=%s written=%s/%s" % (err, written, len(data)))
    finally:
        _winapi.CloseHandle(h)


def _wh_read(path):
    h = _winapi.CreateFile(path, _WH_GENERIC_READ, _WH_FILE_SHARE_READ, 0,
                           _WH_OPEN_EXISTING, _WH_FILE_ATTRIBUTE_NORMAL, 0)
    try:
        chunks = []
        while True:
            data, err = _winapi.ReadFile(h, 65536)
            if err:
                raise OSError("ReadFile err=%s" % err)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        _winapi.CloseHandle(h)


def _wh_exists(path):
    return os.path.exists(path)  # stat-family calls work in FL's context


def _wh_delete(path):
    os.unlink(path)


def _wh_replace(src, dst):
    os.replace(src, dst)


def _file_exists(path):
    if _io_mode == "ctypes":
        return _ct_exists(path)
    if _io_mode == "rawfd":
        return _rf_exists(path)
    if _io_mode == "winhandle":
        return _wh_exists(path)
    return os.path.exists(path)


def _file_read(path):
    if _io_mode == "ctypes":
        return _ct_read(path).decode("utf-8")
    if _io_mode == "rawfd":
        return _rf_read(path).decode("utf-8")
    if _io_mode == "winhandle":
        return _wh_read(path).decode("utf-8")
    # FileIO 2-arg form: proven alive in FL's Documents sandbox (DIAG3).
    f = _io_mod.FileIO(path, "r")
    try:
        return f.read().decode("utf-8")
    finally:
        f.close()


def _file_write(path, text):
    if _io_mode == "ctypes":
        _ct_write(path, text.encode("utf-8"))
        return
    if _io_mode == "rawfd":
        _rf_write(path, text.encode("utf-8"))
        return
    if _io_mode == "winhandle":
        _wh_write(path, text.encode("utf-8"))
        return
    f = _io_mod.FileIO(path, "w")
    try:
        f.write(text.encode("utf-8"))
    finally:
        f.close()


def _file_delete(path):
    if _io_mode == "ctypes":
        _ct_delete(path)
        return
    if _io_mode == "rawfd":
        _rf_delete(path)
        return
    if _io_mode == "winhandle":
        _wh_delete(path)
        return
    os.remove(path)


def _file_replace(src, dst):
    if _io_mode == "ctypes":
        _ct_replace(src, dst)
        return
    if _io_mode == "rawfd":
        _rf_replace(src, dst)
        return
    if _io_mode == "winhandle":
        _wh_replace(src, dst)
        return
    os.replace(src, dst)


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    _file_write(tmp, _reply_bytes(obj).decode("utf-8"))
    try:
        _file_replace(tmp, path)
    except Exception:
        # non-atomic fallback: client tolerates partial reads via retry
        _file_write(path, _reply_bytes(obj).decode("utf-8"))
        try:
            _file_delete(tmp)
        except Exception:
            pass


def _file_poll():
    """Process one file-based command if present. Returns True if handled."""
    global _last_cmd_id
    cmd_path = os.path.join(FILE_DIR, CMD_NAME)
    try:
        if not _file_exists(cmd_path):
            return False
        raw = _file_read(cmd_path)
        try:
            _file_delete(cmd_path)
        except Exception:
            pass  # dedup guard below prevents re-execution if delete fails
    except Exception:
        return False
    try:
        req = json.loads(raw)
    except Exception as exc:
        req = {"cmd": "__parse_error__", "error": str(exc)}
    if not isinstance(req, dict):
        req = {"cmd": "__parse_error__", "error": "request must be a JSON object"}
    # Dedup guard: if the cmd-file delete failed, the same request would be
    # re-read every tick — never execute the same request id twice.
    rid = req.get("id")
    if rid is not None and rid == _last_cmd_id:
        try:
            _file_delete(cmd_path)
        except Exception:
            pass
        return True
    ts = req.get("ts")
    if isinstance(ts, (int, float)) and time.time() - ts > STALE_SECONDS:
        return True  # stale command (e.g. from before an FL restart) — drop it
    try:
        reply = _build_reply(req)
        if isinstance(reply, _Deferred):
            _pending.append((reply, rid, ("file",)))
        else:
            _atomic_write_json(os.path.join(FILE_DIR, REP_NAME), reply)
    except Exception:
        pass
    if rid is not None:
        _last_cmd_id = rid
    return True


def _probe_builtin():
    # Exact DIAG3-proven form: FileIO(path, "w"/"r") in the Documents tree.
    # No delete probe — os.unlink is refused in FL's sandbox (dedup guard
    # makes cmd-file deletion optional).
    probe = os.path.join(FILE_DIR, "console_probe.tmp")
    f = _io_mod.FileIO(probe, "w")
    try:
        f.write(b"ok")
    finally:
        f.close()
    f = _io_mod.FileIO(probe, "r")
    try:
        if f.read() != b"ok":
            raise OSError("builtin readback mismatch")
    finally:
        f.close()


def _probe_rawfd():
    probe = os.path.join(FILE_DIR, "console_probe.tmp")
    _rf_write(probe, b"ok")
    if _rf_read(probe) != b"ok":
        raise OSError("rawfd readback mismatch")
    _rf_delete(probe)


_diag_done = False


def _diagnose_rawfd():
    """One-shot granular probe map v3: discriminate write-block vs path-block.
    DIAG2 proved reads + os.open + FileIO alive (Documents path). All earlier
    failures were WRITES to the AppData FILE_DIR. Test the full matrix."""
    global _diag_done
    if _diag_done:
        return
    _diag_done = True
    out = []

    def t(name, fn):
        try:
            r = fn()
            out.append("%s=OK:%r" % (name, r if isinstance(r, (int, bytes)) else type(r).__name__))
        except BaseException as e:
            out.append("%s=FAIL %s:%s" % (name, type(e).__name__, str(e)[:50]))

    SCRIPT_DIR = r"C:\Users\quick\Documents\Image-Line\FL Studio\Settings\Hardware\AwknodeConsole"
    SCRIPT_PATH = SCRIPT_DIR + r"\device_AwknodeConsole.py"
    import io as _io_mod

    def read_appdata():
        fd = os.open(FILE_DIR + r"\fl_rpc_client.py", 0)
        os.close(fd)
        return fd

    def write_docs():
        p = SCRIPT_DIR + r"\console_wtest.tmp"
        f = _io_mod.FileIO(p, "w")
        f.write(b"ok")
        f.close()
        f = _io_mod.FileIO(p, "r")
        data = f.read()
        f.close()
        os.unlink(p)
        return data

    def write_appdata():
        p = FILE_DIR + r"\console_wtest.tmp"
        f = _io_mod.FileIO(p, "w")
        f.write(b"ok")
        f.close()
        f = _io_mod.FileIO(p, "r")
        data = f.read()
        f.close()
        os.unlink(p)
        return data

    def creat_docs():
        p = SCRIPT_DIR + r"\console_wtest2.tmp"
        fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
        os.write(fd, b"ok")
        os.close(fd)
        os.unlink(p)
        return fd

    def creat_appdata():
        p = FILE_DIR + r"\console_wtest2.tmp"
        fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
        os.write(fd, b"ok")
        os.close(fd)
        os.unlink(p)
        return fd

    def open3_docs():
        fd = os.open(SCRIPT_PATH, 0, 0o600)  # 3 args, read, Documents
        os.close(fd)
        return fd

    t("read_appdata", read_appdata)
    t("open3_docs", open3_docs)
    t("write_docs", write_docs)
    t("write_appdata", write_appdata)
    t("creat_docs", creat_docs)
    t("creat_appdata", creat_appdata)
    print("Awknode Console DIAG3 " + " | ".join(out))


def _probe_ctypes():
    probe = os.path.join(FILE_DIR, "console_probe.tmp")
    _ct_write(probe, b"ok")
    _ct_delete(probe)


def _probe_winhandle():
    if _winapi is None:
        raise OSError("_winapi unavailable (non-Windows)")
    probe = os.path.join(FILE_DIR, "console_probe.tmp")
    _wh_write(probe, b"ok")
    if _wh_read(probe) != b"ok":
        raise OSError("winhandle readback mismatch")
    _wh_delete(probe)


def _write_status(ok, mode, io_mode, errors):
    """Best-effort status file so the relay can verify FL-side state over SSH
    without the owner pasting the Script output window."""
    obj = {"ok": ok, "mode": mode, "io_mode": io_mode,
           "py": sys.version.split()[0], "ts": time.time(),
           "errors": {k: str(v)[:160] for k, v in errors.items() if v}}
    try:
        _atomic_write_json(os.path.join(FILE_DIR, STATUS_NAME), obj)
    except Exception:
        pass


def _start_transport():
    """Pick a working transport. Called lazily from OnIdle — OnInit's context
    may be more restricted than the idle context. Never raises. On total
    failure, OnIdle's backoff gate limits retries to one per
    RETRY_BACKOFF_SECONDS and the log line prints only when the failure
    signature changes (no per-tick spam)."""
    global _sock, _thread, _mode, _io_mode, _next_retry_at, _fail_sig
    errors = {}
    _wsa_startup()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.settimeout(0.5)
        _sock = s
        _thread = threading.Thread(target=_receiver, name="AwknodeConsoleRx", daemon=True)
        _thread.start()
        _mode = "udp"
        print("Awknode Console listening on %s:%d (udp)" % (HOST, PORT))
        _write_status(True, _mode, _io_mode, errors)
        return True
    except Exception as exc:
        errors["udp"] = exc
    # FILE mode: directory already exists (created by the installer) — no mkdir.
    try:
        _probe_builtin()
        _io_mode = "builtin"
        _mode = "file"
        print("Awknode Console FILE mode (builtin io; udp unavailable: %s)" % errors["udp"])
        _write_status(True, _mode, _io_mode, errors)
        return True
    except Exception as exc:
        errors["builtin"] = exc
    # rawfd before ctypes: _ctypes never loads in a subinterpreter, while
    # os.open/os.read/os.write (int fds + bytes) can survive FL's context.
    try:
        _probe_rawfd()
        _io_mode = "rawfd"
        _mode = "file"
        print("Awknode Console FILE mode (rawfd io; udp: %s; builtin: %s)"
              % (errors["udp"], errors["builtin"]))
        _write_status(True, _mode, _io_mode, errors)
        return True
    except Exception as exc:
        errors["rawfd"] = exc
        _diagnose_rawfd()
    try:
        _probe_winhandle()
        _io_mode = "winhandle"
        _mode = "file"
        print("Awknode Console FILE mode (winhandle io; udp: %s; builtin: %s; rawfd: %s)"
              % (errors["udp"], errors["builtin"], errors.get("rawfd")))
        _write_status(True, _mode, _io_mode, errors)
        return True
    except Exception as exc:
        errors["winhandle"] = exc
    try:
        _io_mode = "ctypes"
        _probe_ctypes()
        _mode = "file"
        print("Awknode Console FILE mode (ctypes io; udp: %s; builtin: %s; rawfd: %s; winhandle: %s)"
              % (errors["udp"], errors["builtin"], errors.get("rawfd"), errors.get("winhandle")))
        _write_status(True, _mode, _io_mode, errors)
        return True
    except Exception as exc:
        errors["ctypes"] = exc
        _io_mode = None
    sig = "|".join("%s:%s" % (k, type(v).__name__) for k, v in errors.items())
    _next_retry_at = time.time() + RETRY_BACKOFF_SECONDS
    if sig != _fail_sig:
        _fail_sig = sig
        print("Awknode Console START FAILED (py%s) udp=%s builtin=%s rawfd=%s winhandle=%s ctypes=%s — retry in %ds"
              % (sys.version.split()[0], errors.get("udp"), errors.get("builtin"),
                 errors.get("rawfd"), errors.get("winhandle"), errors.get("ctypes"),
                 RETRY_BACKOFF_SECONDS))
    _write_status(False, None, None, errors)
    return False


def _receiver():
    while not _stop.is_set():
        try:
            data, addr = _sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            req = json.loads(data.decode("utf-8"))
        except Exception as exc:
            req = {"cmd": "__parse_error__", "error": str(exc)}
        _q.put((addr, req))


# ---------- FL Studio script events ----------

def OnInit():
    global _started, _last_err, _next_retry_at, _fail_sig, _diag_done
    _stop.clear()
    _started = False
    _last_err = None
    _next_retry_at = 0.0
    _fail_sig = None
    _diag_done = False
    # No OS calls here: FL's OnInit context can refuse C-level OS operations.
    # Transport setup happens on the first OnIdle instead.
    print("Awknode Console loaded (py%s); transport starts on idle" % sys.version.split()[0])


def OnDeInit():
    global _sock, _mode, _io_mode, _started, _next_retry_at, _fail_sig
    _stop.set()
    del _pending[:]
    try:
        if _sock is not None:
            _sock.close()
    except Exception:
        pass
    _sock = None
    _mode = None
    _io_mode = None
    _started = False
    _next_retry_at = 0.0
    _fail_sig = None
    _hw["loaded"] = False
    _hw["learn"] = None


def OnIdle():
    global _started, _last_err
    try:
        _hw_pad_hold_hints()
        if not _started and time.time() >= _next_retry_at:
            _started = _start_transport()
        if _sock is not None:
            for _ in range(MAX_PER_TICK):
                try:
                    addr, req = _q.get_nowait()
                except queue.Empty:
                    break
                _handle(addr, req)
        # File transport is always polled too (fallback when UDP is unavailable,
        # and a safety net when FL is too busy to pump the UDP queue). Skip when
        # no transport came up — there is nothing to poll and the IO would fail.
        _hw_track_focus()
        if _mode is not None:
            if not _hw["loaded"]:
                _hw_load_active()
            _file_poll()
        _pump_pending()
    except Exception as exc:
        msg = "Awknode Console idle error: %s" % exc
        if msg != _last_err:
            _last_err = msg
            print(msg)


def _hw_selected_midi_out():
    """Channel index of the selected channel when it is a MIDI Out, else None."""
    try:
        ch = channels.selectedChannel()
        if ch >= 0 and plugins.isValid(ch, -1) and \
                str(plugins.getPluginName(ch, -1)).strip().lower() in STRICT_PLUGINS:
            return ch
    except Exception:
        pass
    return None


def _hw_wheel_to_midi_out(event):
    """MIDI Out only forwards notes, so relay the wheels for it: the mod wheel turns
    the preset's MOD knob (CC 1) and the pitch wheel drives the channel's pitch
    (sent on as pitch bend). Other instruments get the wheels from FL directly."""
    ch = _hw_selected_midi_out()
    if ch is None:
        return False
    st = event.status & 0xF0
    rec = {"status": "0x%02X" % event.status, "data1": event.data1, "data2": event.data2,
           "ts": round(time.time(), 3)}
    if st == 0xB0 and event.data1 == 1:
        key = ("MOD", ch)
        p = _param_cache.get(key)
        if p is None:
            p = -1
            for i in range(min(plugins.getParamCount(ch, -1), 80)):
                if str(plugins.getParamName(i, ch, -1)).strip().upper() == "MOD":
                    p = i
                    break
            if p >= 0:
                _param_cache[key] = p
        if p < 0:
            return False
        _hw_set_param(ch, -1, p, "abs", event.data2 / 127.0)
        rec["did"] = "midi out %d MOD=%d" % (ch, event.data2)
    elif st == 0xE0:
        v = (((event.data2 << 7) | event.data1) - 8192) / 8192.0
        v = max(-1.0, min(1.0, v))
        channels.setChannelPitch(ch, v, 0)
        rec["did"] = "midi out %d pitch=%s" % (ch, round(v, 3))
    else:
        return False
    _hw["seen"].append(rec)
    del _hw["seen"][:-MAX_SEEN]
    return True


TAP_SECONDS = 0.6  # agent pads: released sooner = tap, later = hold
_hold_hinted = set()


def _hint(msg):
    try:
        ui.setHintMsg(msg)
    except Exception:
        pass


def _hw_pad_hold_hints():
    """OnIdle: once a safety pad is held long enough, say that releasing = STOP."""
    for cid, t0 in list(_pad_down.items()):
        if cid not in _hold_hinted and time.time() - t0 >= TAP_SECONDS:
            for c in (_hw["profile"] or {}).get("controls", []):
                if c["id"] == cid and (c.get("target") or {}).get("tap") == "undo":
                    _hint("Awknode: release = STOP AUDIO")
            _hold_hinted.add(cid)
    _hold_hinted.intersection_update(_pad_down)
_pad_down = {}  # control id -> press time


def _hw_note_controls():
    by = {}
    for c in (_hw["profile"] or {}).get("controls", []):
        if c.get("note") is not None:
            by[(int(c.get("channel", 0)), int(c["note"]))] = c
    return by


def _hw_agent_event(control, action, extra=None):
    t = control.get("target") or {}
    ev = {"id": control["id"], "name": t.get("name", control["id"]), "action": action,
          "ts": round(time.time(), 3)}
    ev.update(extra or {})
    _hw["events"].append(ev)
    del _hw["events"][:-MAX_EVENTS]
    return ev


def _hw_on_note(event):
    """Pads mapped as agent buttons: press/release events (with hold time) for agents.
    A pad with tap 'undo' undoes one step itself on a short tap (instant, no agent)."""
    st = event.status & 0xF0
    control = _hw_note_controls().get((event.status & 0x0F, event.data1))
    if control is None:
        return False
    rec = {"status": "0x%02X" % st, "channel": event.status & 0x0F, "data1": event.data1,
           "data2": event.data2, "control": control["id"], "ts": round(time.time(), 3)}
    if st == 0x90 and event.data2 > 0:
        _pad_down[control["id"]] = time.time()
        _hw_agent_event(control, "press")
        if (control.get("target") or {}).get("tap") == "undo":
            _hint("Awknode: tap = undo AUDIO's last change, hold = STOP")
        elif (control.get("target") or {}).get("name") == "talk":
            _hint("Awknode: AUDIO listening... release to send")
        rec["did"] = "agent press"
    elif st == 0x80 or st == 0x90:
        held = round(time.time() - _pad_down.pop(control["id"], time.time()), 3)
        gesture = "tap" if held < TAP_SECONDS else "hold"
        extra = {"held": held, "gesture": gesture}
        if gesture == "tap" and (control.get("target") or {}).get("tap") == "undo":
            r = _journal_undo()
            if r:
                extra["did"] = "undo agent"
                _hint("Awknode: put back AUDIO's last change (%d values)" % r["restored"])
            else:
                general.undoUp()
                extra["did"] = "undo"
        elif gesture == "hold" and (control.get("target") or {}).get("tap") == "undo":
            _hint("Awknode: STOP sent to AUDIO")
        elif (control.get("target") or {}).get("name") == "talk":
            _hint("Awknode: sent to AUDIO")
        _hw_agent_event(control, "release", extra)
        rec["did"] = "agent release %s%s" % (gesture, " + undo" if extra.get("did") else "")
    else:
        return True  # pad pressure (aftertouch): swallow quietly
    _hw["seen"].append(rec)
    del _hw["seen"][:-MAX_SEEN]
    return True


def OnMidiIn(event):
    pass  # everything reaches OnMidiMsg; nothing is consumed here


def OnMidiMsg(event):
    """Mapped CCs are consumed; notes and everything unmapped pass through."""
    try:
        if (event.status & 0xF0) == 0xB0:
            if _hw_on_cc(event) or (event.data1 == 1 and _hw_wheel_to_midi_out(event)):
                event.handled = True
        elif (event.status & 0xF0) == 0xE0:
            if _hw_on_pitchbend(event) or _hw_wheel_to_midi_out(event):
                event.handled = True
        elif (event.status & 0xF0) in (0x80, 0x90, 0xA0) and _hw_on_note(event):
            event.handled = True
        else:
            # Log everything else too (notes, pitch bend, Mackie/DAW-mode messages) so
            # hw.seen shows what the hardware really sends. Never consumed.
            _hw["seen"].append({"status": "0x%02X" % (event.status & 0xF0), "channel": event.status & 0x0F,
                                "data1": event.data1, "data2": event.data2, "ts": round(time.time(), 3)})
            del _hw["seen"][:-MAX_SEEN]
    except Exception as exc:
        _hw["error"] = "midi: %s" % exc


# ---------- MMC transport (KeyLab Essential transport buttons outside DAW mode) ----------
# MMC = F0 7F <dev> 06 <cmd> F7. 01 stop, 02 play, 03 deferred play, 06 record strobe,
# 04/05 ff/rewind are logged only. Turn off with "mmc_transport": false in the profile.
_MMC_ACTIONS = {1: "stop", 2: "play", 3: "play", 6: "record"}


def OnSysEx(event):
    try:
        data = bytes(event.sysex or b"")
    except Exception:
        data = b""
    rec = {"status": "SYSEX", "hex": data[:16].hex(" "), "ts": round(time.time(), 3)}
    _hw["seen"].append(rec)
    del _hw["seen"][:-MAX_SEEN]
    try:
        if len(data) >= 6 and data[0] == 0xF0 and data[1] == 0x7F and data[3] == 0x06:
            cmd = data[4]
            rec["mmc"] = cmd
            if (_hw["profile"] or {}).get("mmc_transport", True) and cmd in _MMC_ACTIONS:
                a = _MMC_ACTIONS[cmd]
                if a == "stop":
                    transport.stop()
                elif a == "play":
                    transport.start()
                elif a == "record":
                    transport.record()
                rec["did"] = "transport " + a
                event.handled = True
    except Exception as exc:
        rec["error"] = str(exc)
