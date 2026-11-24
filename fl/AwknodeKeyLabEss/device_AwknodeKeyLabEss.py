# name=Awknode KeyLab Essential
"""Awknode KeyLab Essential: the KeyLab Essential V2 script plus Awknode Console.

- KeyLab Essential V2 (Farès MEZDOUR / Arturia) keeps everything it does: DAW-mode
  transport, faders, main-knob navigation, display, lights, pads -> FPC, and knob
  control of the FL plugins it knows (FLEX, Sytrus, Harmor, ...).
- Awknode Console adds the agent bridge (Claude / Hermes / Codex via awk_console.py),
  knob control of ANY focused plugin (KickNull, AlleyVerb, effects, third-party),
  learn/remap profiles and the Agent A/B buttons.
- The KeyLab's mode is detected: fader pitch-bend = DAW mode -> profile keylab61-daw;
  Analog Lab knob/fader CCs -> profile keylab61.
The originals (KeyLab Essential V2, Awknode Console, Hermes FL Bridge) are untouched.
"""
import json

import ui

import AwknodeConsoleCore as AC
import KeyLabEssProcess
import KeyLabEssV2Core as V2

# FL plugins the V2 script maps itself (from KeyLabEssPlugin.py)
V2_PLUGINS = {"3x Osc", "BASSDRUM", "FL Keys", "FLEX", "FPC", "Fruit kick", "Fruity DX10", "GMS",
              "Harmless", "Harmor", "MiniSynth", "Morphine", "PoiZone", "Sakura", "Sytrus"}
# CCs the KeyLab only sends in Analog Lab mode (knobs 1-5, faders 1-9)
ANALOG_LAB_ONLY = {74, 71, 76, 77, 93, 73, 75, 79, 72, 80, 81, 82, 83, 85}
PROFILE_ANALOG_LAB = "keylab61"
PROFILE_DAW = "keylab61-daw"            # V2 mixer mode: faders = mixer volumes, knobs = V2 pans
PROFILE_DAW_CHANNEL = "keylab61-daw-channel"  # V2 channel mode: channel rack pans / volumes
DAW_KNOB_CCS = set(range(16, 24))

_v2_err = [None]


def _v2(fn_name, *args):
    """Call a V2 hook; a V2 error never takes the agent bridge down with it."""
    fn = getattr(V2, fn_name, None)
    if fn is None:
        return
    try:
        fn(*args)
    except Exception as exc:
        msg = "KeyLab V2 %s error: %s" % (fn_name, exc)
        if msg != _v2_err[0]:
            _v2_err[0] = msg
            print(msg)


def _switch_profile(name):
    hw = AC._hw
    if not hw["loaded"] or hw["learn"] is not None or hw["name"] == name:
        return
    try:
        AC._hw_apply_profile(name, json.loads(AC._file_read(AC._hw_profile_path(name))))
        print("Awknode KeyLab: mode -> profile '%s'" % name)
    except Exception as exc:
        hw["error"] = "auto profile switch to %s failed: %s" % (name, exc)


def _mixer_mode():
    return getattr(KeyLabEssProcess, "MIXER_MODE", 0) == 1


def _v2_owns_focus():
    """V2's mixer mode: V2 keeps the knobs (mixer pans)."""
    return _mixer_mode()


def OnInit():
    AC.OnInit()
    _v2("OnInit")


def OnDeInit():
    _v2("OnDeInit")
    AC.OnDeInit()


def OnIdle():
    AC.OnIdle()
    _v2("OnIdle")


def OnRefresh(flags):
    _v2("OnRefresh", flags)


def OnUpdateBeatIndicator(value):
    _v2("OnUpdateBeatIndicator", value)


def OnSysEx(event):
    AC.OnSysEx(event)
    if not event.handled:
        _v2("OnSysEx", event)


def OnMidiIn(event):
    pass


def OnMidiMsg(event):
    st = event.status & 0xF0
    if st == 0xE0 or (st == 0xB0 and event.data1 in DAW_KNOB_CCS and AC._hw["name"] != PROFILE_ANALOG_LAB):
        _switch_profile(PROFILE_DAW if _mixer_mode() else PROFILE_DAW_CHANNEL)
    elif st == 0xB0 and event.data1 in ANALOG_LAB_ONLY:
        _switch_profile(PROFILE_ANALOG_LAB)
    # Faders follow V2's mixer bank while V2's mixer mode is on.
    if st == 0xE0 and getattr(KeyLabEssProcess, "MIXER_MODE", 0) == 1:
        try:
            AC._hw["bank"] = int(KeyLabEssProcess.AKLESS.MX_OFFSET)
        except Exception:
            pass
    # A V2-known FL plugin in focus keeps V2's own knob mapping (not while learning).
    if not (st == 0xB0 and AC._hw["learn"] is None and _v2_owns_focus()):
        AC.OnMidiMsg(event)
        if event.handled:
            return
    _v2("OnMidiMsg", event)
