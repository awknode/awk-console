# Awk Console for FL Studio

Control FL Studio from an **Arturia KeyLab Essential 61 (mk1)**, with knobs and faders that follow whatever plugin you're working on. It can also be driven by **AI agents** (Claude, Codex or anything that speaks MCP).

## What you need
- Windows with **FL Studio 2026** (the scripts use FL's Python MIDI scripting).
- An Arturia KeyLab Essential 61 (mk1). Other controllers can use the `generic` profile and *learn* mode.
- *Optional, for AI agents:* Python 3.10 or newer.

## Install
1. Unzip this folder anywhere.
2. Double-click **Install.bat**.
   - It copies two FL scripts into `Documents\Image-Line\FL Studio\Settings\Hardware`. Any older copies are kept as `.backup-<date>`.
   - It finds Arturia's *KeyLab Essential V2* script in your FL Studio install and copies the parts it needs (they're Arturia's, so they aren't shipped here).
   - It puts the agent connector in `Documents\Awknode\Console`.
3. If it says Arturia's script wasn't found, Plugin mode still works. For DAW mode, download the KeyLab Essential FL Studio script from Arturia's KeyLab Essential downloads, unzip it, and run:
   `powershell -ExecutionPolicy Bypass -File install.ps1 -ArturiaDir "C:\path\to\that\folder"`
4. If your FL user-data folder isn't under Documents, add `-FLHardware "X:\...\FL Studio\Settings\Hardware"`.

## Set up FL Studio
**Options → MIDI settings → Input:**

| Port | Controller type | Port number |
|---|---|---|
| Arturia KeyLab Essential 61 (main port) | **Awknode Console** | 10 |
| MIDIIN2 (KeyLab Essential 61) | **Awknode KeyLab Essential** | 11 |

Don't put *Awknode KeyLab Essential* on the main port: the keys stop playing notes.

## Using it
| On the KeyLab | Knobs 1–8 | Faders 1–8 |
|---|---|---|
| **Analog Lab / User 1** (Plugin mode) | controls 1–8 of the plugin you last clicked | controls 9–16 |
| **DAW → Mixer** | mixer pans | mixer volumes |
| **DAW → Channel** | channel-rack pans | channel-rack volumes |

- Knob 9 and fader 9 control the selected mixer track's pan and volume.
- Mode switches automatically depending on which buttons and knobs you use.
- DAW mode also gives you transport (Play acts on release), the main knob, the display, and pads that play FPC.

## AI agents (optional)
The connector is `Documents\Awknode\Console\awk_console.py` (standard-library Python, nothing to install).
- One command: `python awk_console.py call hw.status`
- As an MCP server, add this to your agent's MCP config:
  ```json
  {"mcpServers": {"awk-console": {"command": "python",
    "args": ["C:\\Users\\YOU\\Documents\\Awknode\\Console\\awk_console.py", "mcp"]}}}
  ```
  Tools: `fl_status`, `fl_help`, `fl_call`, `hw_learn`, `hw_map`, `hw_events`, plus `fl_mix_review` and the studio macros.
- If your Documents folder is redirected (OneDrive), set `AWK_CONSOLE_IO` to the script's `io` folder.

## Troubleshooting
- **Script not in FL's list:** restart FL Studio, then check the folders landed in `...\Settings\Hardware`.
- **Knobs do nothing:** make sure the main port uses *Awknode Console*, then click a plugin window so it's "focused".
- **After editing a script:** use FL's *Reload script* in MIDI settings.

See NOTICE.txt for credits.
