<img src="assets/icon.svg" width="64" height="64" alt="">

# save-all-models-output

Records every AI conversation on a machine (prompts, answers, thinking, tool calls and tool results) into one SQLite file:

**`Documents/local-data/ai_history-<machine>.sqlite`** on macOS, Linux and Windows.

Upload or back up that file however you like. Nothing is uploaded automatically.

## Set up a machine (one command)

**macOS / Linux**
```sh
git clone https://github.com/diwas10/save-all-models-output.git ~/save-all-models-output && ~/save-all-models-output/setup.sh
```

**Windows**
```bat
git clone https://github.com/diwas10/save-all-models-output.git %USERPROFILE%\save-all-models-output && %USERPROFILE%\save-all-models-output\setup.cmd
```

After that, everything runs in the background and starts at login. Running setup again is safe. Options: `--no-proxy` skips the macOS desktop-app proxy, `--dry-run` shows what setup would do, and `uninstall.sh` / `uninstall.cmd` removes the background jobs (your data is kept).

| | macOS | Linux | Windows |
|---|---|---|---|
| Scheduler | launchd | systemd `--user` (cron if there's no systemd; keeps running while logged out via linger) | Task Scheduler + Startup folder (no admin needed) |
| Needs | Python 3.9+ (`xcode-select --install`) | `python3`, `git` | Python 3.9+ ([python.org](https://www.python.org/downloads/)), `git` |

Each machine runs a daily `git pull` and re-runs setup when the code changed, so pushing to `main` updates every machine.

Things a script can't do for you:
- **Load the browser extension** (about 30 seconds). Setup prints the steps.
- **macOS desktop proxy only:** macOS asks for your password once to trust the proxy certificate.

## What gets recorded

| Source | How |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` (collector, every 10 min) |
| Codex | `~/.codex/sessions/**/*.jsonl` |
| Antigravity | `~/.gemini/antigravity/conversations/*.db` (protobuf, decoded) plus its search index for older encrypted chats |
| OpenCode | `~/.local/share/opencode/opencode.db` (or `$XDG_DATA_HOME/opencode`), plus the JSON `storage/` folder used by older versions |
| chatgpt.com, claude.ai, gemini.google.com | browser extension → local server `127.0.0.1:8765` |
| ChatGPT / Gemini / Claude desktop apps | macOS only: mitmproxy on `127.0.0.1:8080`, AI hosts only |
| Past chats (ChatGPT, Claude, Gemini) | the export zip each service emails you (see below) |

### Importing past chats

Request an export from ChatGPT (Settings → Data controls → Export), Claude (Settings → Privacy → Export data) or Google Takeout (select **My Activity → Gemini Apps**, plus **Gemini** and **Gemini in Workspace** if listed). Then either:

- drop the `.zip` into `Documents/local-data`, and the next background run (within 10 minutes) imports it once, or
- import it now: `python3 collectors/exports.py ~/Downloads/takeout-*.zip`

Re-importing is safe: an export that overlaps what you already have doesn't create duplicates. Only text is imported. Attachments are listed by name and stay in the zip.

## Data

- `~/AIHistory/ai_history.sqlite` is the live database (with logs in `~/AIHistory/logs/`).
- `Documents/local-data/ai_history-<machine>.sqlite` is a consistent snapshot, rewritten at most hourly and only when something changed. Copy this one.
- **Combining machines:** copy other machines' `ai_history-*.sqlite` files into your `Documents/local-data`. The next run merges them in without creating duplicates, and the `machine` column shows where each row came from.

Tables: `conversations` (source, title, model, project, machine), `messages` (role: user/assistant/tool; kind: text/thinking/tool_call/tool_result; content; model; created_at), `captures` (raw browser and proxy traffic; headers and cookies are never stored).

```sh
python3 server/server.py stats      # counts
python3 collectors/run.py           # collect + snapshot right now
python3 server/server.py reparse    # rebuild messages after a parser fix
```

**Privacy:** tool results include whatever agents read, which can include `.env` files and API keys. Treat the snapshot as a secret. On macOS, never share `~/.mitmproxy/mitmproxy-ca-key.pem`.

## Layout

```
setup.sh / setup.cmd      entry points → scripts/setup.py (all OSes)
scripts/                  setup.py · job.py (runs each background job) · update.py
collectors/               Claude Code, Codex, Antigravity, OpenCode, export-zip importers + snapshot/merge
server/                   local receiver for the extension; shared storage + parsers
extension/                Chrome + Firefox extension (python3 extension/build.py)
desktop/                  macOS mitmproxy addon + install
```

The parsers read undocumented internal formats, so they are best-effort. Raw captures are always kept, so a fixed parser can be re-run.
