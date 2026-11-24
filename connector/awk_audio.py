"""Awknode audio + notes: analyse a bounce/stem, pull a melody to MIDI, paste notes into FL.

- audio.analyze {path}: duration, peak, integrated loudness (LUFS, BS.1770-style
  K-weighting + gating, estimate), tempo candidates, estimated key, band balance.
- audio.melody {path, out?}: monophonic pitch track -> notes -> .mid (best on a
  solo stem; on a full mix it's a sketch).
- notes.paste {notes, name?, mode?}: writes a .mid to Documents\\Awknode\\MIDI and
  the piano-roll script "Awknode Paste" with the notes baked in. Stacks opens
  the channel's piano roll > Tools (scripts menu) > Awknode Paste. Nothing takes
  over the screen: FL's pyscript sandbox can't read files and the FL API can't
  write notes from a controller script, so this is the one-click path.

Ideas from fls-pilot (zorroukk, MIT): Krumhansl-Schmuckler key profiles, notes
baked into a generated .pyscript. Fresh code: numpy + ffmpeg only (no librosa).
"""

import json
import os
import re
import shutil
import struct
import subprocess

AUDIO_SR = 22050
HOME = os.path.expanduser("~")
MIDI_DIR = os.path.join(HOME, "Documents", "Awknode", "MIDI")
PR_SCRIPTS = os.path.join(HOME, "Documents", "Image-Line", "FL Studio", "Settings",
                          "Piano roll scripts")
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


class AudioError(Exception):
    pass


def _np():
    try:
        import numpy as np
        return np
    except ImportError:
        raise AudioError("numpy is needed for audio analysis (pip install numpy)")


def _ffmpeg():
    for c in (shutil.which("ffmpeg"), r"C:\ffmpeg\bin\ffmpeg.exe"):
        if c and os.path.exists(c):
            return c
    raise AudioError("ffmpeg not found")


def load(path, sr=AUDIO_SR, stereo=False):
    """Any audio file -> float32 array (mono, or (n, 2) when stereo) via ffmpeg."""
    np = _np()
    if not os.path.isfile(path):
        raise AudioError("no such file: %s" % path)
    ch = 2 if stereo else 1
    p = subprocess.run([_ffmpeg(), "-v", "error", "-i", path, "-f", "f32le", "-ac", str(ch),
                        "-ar", str(sr), "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=300)
    if p.returncode != 0:
        raise AudioError("ffmpeg: %s" % p.stderr.decode("utf-8", "replace")[-300:])
    y = np.frombuffer(p.stdout, dtype=np.float32)
    if stereo:
        y = y[: len(y) // 2 * 2].reshape(-1, 2)
    if len(y) == 0:
        raise AudioError("no audio decoded")
    return y


def _frames(np, y, n, hop):
    if len(y) < n:
        y = np.pad(y, (0, n - len(y)))
    count = 1 + (len(y) - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(count)[:, None]
    return y[idx]


# ---------------------------------------------------------------- loudness

def _k_weight_power(np, freqs):
    """|H(f)|^2 of the BS.1770 K-weighting (shelf + RLB high-pass), analogue approximation."""
    f = np.maximum(freqs, 1e-3)
    # high shelf ~ +4 dB above ~1.5 kHz
    shelf_db = 4.0 / (1.0 + (1681.97 / f) ** 2)
    # RLB: 2nd-order high-pass at 38 Hz
    hp = (f / 38.13) ** 4 / (1.0 + (f / 38.13) ** 4)
    return (10 ** (shelf_db / 10.0)) * hp


def lufs(np, stereo, sr):
    n = int(0.4 * sr)
    hop = n // 4
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    w = _k_weight_power(np, freqs)
    z = None
    for c in range(stereo.shape[1]):
        fr = _frames(np, stereo[:, c], n, hop)
        X = np.abs(np.fft.rfft(fr, axis=1)) ** 2
        X[:, 1:-1] *= 2.0
        p = (X * w).sum(axis=1) / (n * n)
        z = p if z is None else z + p
    lk = -0.691 + 10 * np.log10(np.maximum(z, 1e-12))
    g = z[lk > -70.0]
    if len(g) == 0:
        return None
    rel = -0.691 + 10 * np.log10(g.mean()) - 10.0
    g2 = z[(lk > -70.0) & (lk > rel)]
    return round(float(-0.691 + 10 * np.log10(g2.mean())), 1) if len(g2) else None


# ---------------------------------------------------------------- tempo / key / bands

def tempo(np, y, sr):
    n, hop = 2048, 512
    S = np.abs(np.fft.rfft(_frames(np, y, n, hop) * np.hanning(n), axis=1))
    S = np.log1p(S)
    flux = np.maximum(0.0, np.diff(S, axis=0)).sum(axis=1)
    flux = flux - flux.mean()
    if len(flux) < 64:
        return []
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    fps = sr / float(hop)
    cands = []
    for bpm in np.arange(60.0, 200.0, 0.5):
        lag = fps * 60.0 / bpm
        i = int(round(lag))
        if i + 1 >= len(ac):
            continue
        score = ac[i] + 0.5 * ac[min(len(ac) - 1, 2 * i)]
        cands.append((score, bpm))
    cands.sort(reverse=True)
    out = []
    for s, b in cands:
        if all(abs(b - o) > 3 for o in out):
            out.append(float(b))
        if len(out) == 3:
            break
    return out


def key(np, y, sr):
    n, hop = 8192, 4096
    fr = _frames(np, y, n, hop) * np.hanning(n)
    S = np.abs(np.fft.rfft(fr, axis=1)).mean(axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    m = (freqs > 55) & (freqs < 2000)
    midi = 69 + 12 * np.log2(freqs[m] / 440.0)
    pc = np.mod(np.round(midi).astype(int), 12)
    chroma = np.bincount(pc, weights=S[m] ** 2, minlength=12)
    best = None
    for i in range(12):
        for mode, prof in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            r = float(np.corrcoef(np.roll(prof, i), chroma)[0, 1])
            if best is None or r > best[0]:
                best = (r, NOTE_NAMES[i], mode)
    return {"key": "%s %s" % (best[1], best[2]), "confidence": round(best[0], 2), "estimated": True}


BANDS = [("sub", 20, 60), ("low", 60, 250), ("low_mid", 250, 500), ("mid", 500, 2000),
         ("high_mid", 2000, 6000), ("air", 6000, 11000)]


def bands(np, y, sr):
    n = 4096
    S = (np.abs(np.fft.rfft(_frames(np, y, n, n) * np.hanning(n), axis=1)) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    tot = S[(freqs >= 20) & (freqs < 11000)].sum() or 1.0
    return {name: round(float(100 * S[(freqs >= lo) & (freqs < hi)].sum() / tot), 1)
            for name, lo, hi in BANDS}


def analyze(a):
    np = _np()
    path = a["path"]
    st = load(path, 44100, stereo=True)
    mono44 = st.mean(axis=1)
    y = load(path, AUDIO_SR)
    peak = float(np.abs(st).max())
    rms = float(np.sqrt((mono44 ** 2).mean()))
    side = st[:, 0] - st[:, 1]
    mid = st[:, 0] + st[:, 1]
    corr = float(np.dot(st[:, 0], st[:, 1]) /
                 (np.sqrt((st[:, 0] ** 2).sum() * (st[:, 1] ** 2).sum()) or 1.0))
    db = lambda v: round(20 * float(np.log10(max(v, 1e-9))), 1)
    out = {
        "path": path,
        "duration_s": round(len(mono44) / 44100.0, 2),
        "peak_dbfs": db(peak),
        "rms_dbfs": db(rms),
        "lufs_integrated": lufs(np, st, 44100),
        "crest_db": round(db(peak) - db(rms), 1),
        "stereo_correlation": round(corr, 2),
        "side_vs_mid_db": round(db(float(np.sqrt((side ** 2).mean()))) -
                                db(float(np.sqrt((mid ** 2).mean()))), 1),
        "tempo_bpm_candidates": tempo(np, y, AUDIO_SR),
        "key": key(np, y, AUDIO_SR),
        "band_balance_pct": bands(np, y, AUDIO_SR),
        "notes": "LUFS/key/tempo are estimates (numpy, no librosa). Tempo lists half/double-time "
                 "candidates; trap often reads as 70-80 or 140-160.",
    }
    if peak >= 0.999:
        out["warning"] = "peaks hit 0 dBFS: the file may be clipped"
    return out


# ---------------------------------------------------------------- melody -> notes

def melody_notes(np, y, sr, lo=36, hi=96, min_len=0.08):
    """Harmonic-sum pitch tracking. Returns [(pitch, start_s, len_s, vel 0..1)]."""
    n, hop = 4096, 256
    fr = _frames(np, y, n, hop) * np.hanning(n)
    S = np.abs(np.fft.rfft(fr, axis=1))
    binhz = sr / float(n)
    pitches = np.arange(lo, hi + 1)
    f0 = 440.0 * 2 ** ((pitches - 69) / 12.0)
    sal = np.zeros((S.shape[0], len(pitches)))
    for h in range(1, 6):
        b = np.round(f0 * h / binhz).astype(int)
        ok = b < S.shape[1]
        sal[:, ok] += S[:, b[ok]] * (0.8 ** (h - 1))
    best = sal.argmax(axis=1)
    strength = sal.max(axis=1)
    energy = np.sqrt((fr ** 2).mean(axis=1))
    gate = max(1e-4, float(np.percentile(energy, 40)))
    voiced = energy > gate
    track = np.where(voiced, pitches[best], -1)
    # smooth: 5-frame median
    k = 5
    pad = np.pad(track, (k // 2, k // 2), mode="edge")
    track = np.array([int(np.median(pad[i:i + k])) for i in range(len(track))])
    t = hop / float(sr)
    notes, cur, start = [], None, 0
    peakv = float(strength.max()) or 1.0
    for i, p in enumerate(list(track) + [-1]):
        if p != cur:
            if cur is not None and cur > 0 and (i - start) * t >= min_len:
                v = float(np.mean(strength[start:i])) / peakv
                notes.append((int(cur), start * t + n / 2.0 / sr, (i - start) * t,
                              round(0.4 + 0.55 * min(1.0, v), 2)))
            cur, start = p, i
    return notes


def melody(a):
    np = _np()
    y = load(a["path"], AUDIO_SR)
    bpm = float(a.get("bpm") or (tempo(np, y, AUDIO_SR) or [120.0])[0])
    raw = melody_notes(np, y, AUDIO_SR)
    beat = 60.0 / bpm
    grid = float(a.get("grid_beats", 0.25))
    notes = []
    for p, s, l, v in raw:
        sb = round(s / beat / grid) * grid
        lb = max(grid, round(l / beat / grid) * grid)
        notes.append({"pitch": p, "start": sb, "length": lb, "velocity": v})
    name = a.get("name") or os.path.splitext(os.path.basename(a["path"]))[0] + " melody"
    path = write_midi(notes, os.path.join(MIDI_DIR, _safe(name) + ".mid"), bpm)
    return {"bpm_used": bpm, "notes": len(notes), "midi": path,
            "note": "drag the .mid into FL, or fl_call notes.paste with these notes",
            "first_notes": notes[:16]}


# ---------------------------------------------------------------- MIDI file + piano-roll paste

PPQ = 960


def _vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def write_midi(notes, path, bpm=120.0):
    """notes: [{pitch, start (beats), length (beats), velocity 0..1}] -> type-0 .mid."""
    ev = []
    for n in notes:
        on = int(round(float(n["start"]) * PPQ))
        off = on + max(1, int(round(float(n["length"]) * PPQ)))
        vel = max(1, min(127, int(round(float(n.get("velocity", 0.8)) * 127))))
        ev.append((on, 1, bytes([0x90, int(n["pitch"]), vel])))
        ev.append((off, 0, bytes([0x80, int(n["pitch"]), 0])))
    ev.sort(key=lambda e: (e[0], e[1]))
    tempo_us = int(round(60000000.0 / float(bpm)))
    body = b"\x00\xff\x51\x03" + tempo_us.to_bytes(3, "big")
    last = 0
    for t, _, data in ev:
        body += _vlq(t - last) + data
        last = t
    body += b"\x00\xff\x2f\x00"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, PPQ))
        f.write(b"MTrk" + struct.pack(">I", len(body)) + body)
    return path


PASTE_SCRIPT = '''# Awknode Paste: generated by Awknode Console (awk_audio.py). Notes are baked in;
# re-generated on every notes.paste. Piano roll > scripts menu > Awknode Paste.
# Title: {title}
import flpianoroll as flp

MODE = {mode!r}     # "replace" clears this piano roll first, "add" keeps what's there
NOTES = {notes!r}   # (pitch, start_beats, length_beats, velocity 0..1)


def _run():
    s = flp.score
    if MODE == "replace":
        try:
            s.clearNotes(True)
        except TypeError:
            s.clearNotes()
    ppq = s.PPQ
    for pitch, start, length, vel in NOTES:
        n = flp.Note()
        n.number = int(pitch)
        n.time = int(round(start * ppq))
        n.length = max(1, int(round(length * ppq)))
        n.velocity = float(vel)
        s.addNote(n)


_run()
'''


def _safe(name):
    return re.sub(r"[^A-Za-z0-9 _.-]+", "", name).strip() or "awknode"


def notes_paste(a):
    """{notes: [{pitch, start, length, velocity}] (beats), name?, bpm?, mode?: replace|add}"""
    notes = a.get("notes") or []
    if not notes:
        raise AudioError("notes: [{pitch, start, length, velocity}] in beats")
    for n in notes:
        if not (0 <= int(n["pitch"]) <= 127):
            raise AudioError("pitch out of range: %r" % n)
    name = _safe(a.get("name") or "Awknode paste")
    mode = a.get("mode", "replace")
    if mode not in ("replace", "add"):
        raise AudioError("mode: replace | add")
    mid = write_midi(notes, os.path.join(MIDI_DIR, name + ".mid"), float(a.get("bpm", 150)))
    tuples = [(int(n["pitch"]), round(float(n["start"]), 4), round(float(n["length"]), 4),
               round(float(n.get("velocity", 0.8)), 3)) for n in notes]
    src = PASTE_SCRIPT.format(title=name, mode=mode, notes=tuples)
    os.makedirs(PR_SCRIPTS, exist_ok=True)
    script = os.path.join(PR_SCRIPTS, "Awknode Paste.pyscript")
    with open(script, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    with open(os.path.join(MIDI_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "bpm": a.get("bpm", 150), "notes": notes}, f, indent=1)
    beats = max(float(n["start"]) + float(n["length"]) for n in notes)
    return {"notes": len(notes), "bars": round(beats / 4.0, 2), "midi": mid, "script": script,
            "how": "open the channel's piano roll > scripts menu (Tools > Scripting) > Awknode Paste. "
                   "Mode %s. Or drag the .mid in." % mode}


MACROS = {"audio.analyze": analyze, "audio.melody": melody, "notes.paste": notes_paste}
