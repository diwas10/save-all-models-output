<img src="assets/icon.svg" width="64" height="64" alt="">

# save-all-models-output

Records every AI conversation you have on your Macs (prompts, answers, thinking, tool calls and tool results) into one SQLite file that Google Drive backs up.

## Set up a Mac (one command)

```sh
git clone https://github.com/diwas10/save-all-models-output.git ~/save-all-models-output && ~/save-all-models-output/setup.sh
```

That's it. Everything then runs in the background and starts at login. Running `setup.sh` again is safe. Use `./setup.sh --no-proxy` to skip the desktop-app proxy, and `./uninstall.sh` to remove everything (your data is kept).

Three things that can't be scripted:
1. **macOS asks for your password once**, to trust the proxy certificate used for desktop-app recording.
2. **Load the browser extension** (about 30 seconds). Browsers don't let scripts install extensions. `setup.sh` prints the steps and opens the folder.
3. **Google Drive:** in Drive for desktop, open Preferences → My Mac → Add folder, and choose `~/AI/local-data`.

Updates: each Mac runs `git pull` daily and re-runs setup if anything changed, so pushing to `main` updates all your laptops.

## What gets recorded

| Source | How | Setup part |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | collector, every 10 min |
| Codex | `~/.codex/sessions/**/*.jsonl` | collector |
| Antigravity | `~/.gemini/antigravity/conversations/*.db` (protobuf, decoded) plus its search index for older encrypted chats | collector |
| chatgpt.com, claude.ai, gemini.google.com | browser extension → local server `127.0.0.1:8765` | server |
| ChatGPT / Gemini / Claude Mac apps | mitmproxy on `127.0.0.1:8080`, AI hosts only | desktop proxy |
| Past chats | official exports (importers to come) | — |

## Where the data is

- `~/AIHistory/ai_history.sqlite` is the live database. It never leaves the Mac.
- `~/AI/local-data/ai_history-<machine>.sqlite` is a consistent snapshot, rewritten at most hourly and only when something changed. This is the file Google Drive syncs.

**Several laptops:** each Mac writes only its own `ai_history-<machine>.sqlite`, so two laptops never write the same file. Each Mac also imports the other laptops' files it finds in that folder, so if the folder is shared across your Macs in Drive, every file ends up holding the history from all of them. The `machine` column tells you which laptop a row came from.

Tables: `conversations` (source, title, model, project, machine), `messages` (role: user/assistant/tool; kind: text/thinking/tool_call/tool_result; content; model; created_at), `captures` (raw browser and proxy traffic; headers and cookies are never stored).

```sh
python3 server/server.py stats      # counts
python3 collectors/run.py           # collect + snapshot right now
python3 server/server.py reparse    # rebuild messages after a parser fix
tail -f ~/AIHistory/logs/*.log      # what the background jobs are doing
```

## Privacy

Tool results include whatever your agents read, which can include `.env` files and API keys, and the snapshot goes to Google Drive. The proxy's private key `~/.mitmproxy/mitmproxy-ca-key.pem` stays on each Mac. Never commit or share it.

## Layout

```
setup.sh / uninstall.sh   one-command install / removal
collectors/               Claude Code, Codex, Antigravity importers + snapshot/merge
server/                   local receiver for the extension; shared storage + parsers
extension/                Chrome + Firefox extension (python3 extension/build.py)
desktop/                  mitmproxy addon + install for the Mac apps
scripts/update.sh         daily self-update
```

The parsers read undocumented internal formats, so they are best-effort. Raw captures are always kept, so a fixed parser can be re-run.
