#!/usr/bin/env python3
"""Build the Chrome and Firefox versions of the extension into extension/dist/.

The code is shared; only the manifest differs (background key and the
Firefox add-on id).  Usage:  python3 extension/build.py
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"

SITES = ["https://chatgpt.com/*", "https://*.chatgpt.com/*", "https://claude.ai/*", "https://gemini.google.com/*"]

BASE = {
    "manifest_version": 3,
    "name": "AI Chat Recorder",
    "version": "0.1.0",
    "description": "Saves your ChatGPT, Claude and Gemini web chats to a server on your own Mac.",
    "permissions": ["storage", "alarms"],
    "host_permissions": SITES + ["http://127.0.0.1:8765/*"],
    "action": {"default_popup": "popup.html", "default_title": "AI Chat Recorder"},
    "content_scripts": [
        {"matches": SITES, "js": ["inject.js"], "run_at": "document_start", "world": "MAIN", "all_frames": False},
        {"matches": SITES, "js": ["content.js"], "run_at": "document_start", "all_frames": False},
    ],
}


def build(target: str) -> Path:
    out = DIST / target
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    manifest = json.loads(json.dumps(BASE))
    if target == "chrome":
        manifest["background"] = {"service_worker": "background.js"}
        manifest["minimum_chrome_version"] = "111"  # world: MAIN content scripts
    else:
        manifest["background"] = {"scripts": ["background.js"]}
        manifest["browser_specific_settings"] = {
            "gecko": {
                "id": "ai-chat-recorder@local",
                "strict_min_version": "128.0",  # world: MAIN content scripts
                "data_collection_permissions": {"required": ["none"]},
            }
        }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return out


if __name__ == "__main__":
    for t in ("chrome", "firefox"):
        print("built", build(t))
