#!/usr/bin/env python3
"""
AI Storage Optimizer
--------------------
Scans your drives, finds the biggest folders/files that are *safe to remove*
(apps, games, caches, old installers, build artifacts, VM images...), and asks
an LLM of your choice to judge which ones are removable. OS-critical locations
(e.g. C:\\Windows, System32, pagefile.sys) are never scanned or sent.

Privacy: only a short list of large items is sent - a redacted path (your home
folder becomes "~", your username becomes <USER>), size, file count and age.
No file contents, no small files, no account info. With --privacy strict,
every folder name that is not a well-known generic name (AppData, node_modules,
Steam, Temp...) is replaced by "<dir>". Use --show-payload to see exactly what
would be sent without sending anything.

LLM setup (key is read from the environment or a .env file next to this script,
never from the command line):
  OpenAI-compatible (OpenAI, OpenRouter, Groq, Mistral, Gemini, Ollama, LM Studio...):
      set LLM_API_KEY=...            (or OPENAI_API_KEY)
      python storage_optimizer.py --model gpt-5 --base-url https://api.openai.com/v1
      python storage_optimizer.py --model llama3.1 --base-url http://localhost:11434/v1
  Anthropic (pip install anthropic):
      set ANTHROPIC_API_KEY=...      (or LLM_API_KEY)
      python storage_optimizer.py --provider anthropic
  Claude Code (pip install claude-agent-sdk) - uses your Claude Code login, no API key:
      python storage_optimizer.py --provider claude-code

Options: --roots C:\\ D:\\   --min-gb 0.5   --max-items 250   --privacy strict
         --show-payload   --demo (print the full LLM messages, send nothing)
         --yes (skip send confirmation)   --claude-path (specific claude executable)
         --clean (interactively move "delete" recommendations to the Recycle Bin; needs: pip install send2trash)
"""
import argparse
import getpass
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GB = 1024 ** 3
REPARSE_POINT = 0x400                              # junctions / symlinks: skip, avoids double counting
CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000           # OneDrive/Dropbox placeholders take no real space

if os.name == "nt":
    PROTECTED_DIRS = ["Windows", "System Volume Information", "Recovery", "Boot", "$WinREAgent",
                      "Config.Msi", r"Program Files\WindowsApps", r"ProgramData\Microsoft\Windows",
                      r"Program Files\Common Files", r"Program Files (x86)\Common Files"]
    PROTECTED_FILES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log", "dumpstack.log.tmp"}
else:
    PROTECTED_DIRS = ["proc", "sys", "dev", "run", "boot", "usr", "bin", "sbin", "lib", "lib32", "lib64",
                      "etc", "System", "private/var/vm", "private/var/db", "Volumes", "mnt", "media"]
    PROTECTED_FILES = set()

WELL_KNOWN = {n.lower() for n in [
    "~", "users", "program files", "program files (x86)", "programdata", "appdata", "local", "locallow",
    "roaming", "temp", "tmp", "downloads", "documents", "desktop", "videos", "pictures", "music", "movies",
    "cache", "caches", ".cache", "dxcache", "glcache", "nv_cache", "d3dscache", "inetcache", "gpucache",
    "code cache", "cachestorage", "service worker", "shadercache", "logs", "crashdumps", "node_modules", ".npm", ".yarn", ".pnpm-store",
    ".gradle", ".m2", ".nuget", "packages", "pip", ".venv", "venv", "env", "__pycache__", "target", "build",
    "dist", "steam", "steamapps", "common", "workshop", "epic games", "gog galaxy", "riot games", "origin",
    "ea games", "ubisoft", "battle.net", "docker", "wsl", "$recycle.bin", "windows.old", "trash", ".trash",
    "library", "application support", "containers", "deriveddata", "xcode", "developer", ".android", "avd",
    "android", "sdk", ".local", "share", "onedrive", "google", "chrome", "microsoft", "mozilla", "firefox",
    "edge", "jetbrains", "code", "discord", "spotify", "slack", "teams", "zoom", "nvidia", "nvidia corporation",
    "amd", "intel", "adobe", "unity", "unreal engine", "anaconda3", "miniconda3", "conda", "envs", "pkgs",
    ".ollama", "ollama", "models", "huggingface", "lm studio", ".lmstudio", "backup", "backups", "installer",
    "installers", "setup", "updates", "softwaredistribution", "obs-studio", "virtualbox vms", "vmware",
    ".vagrant.d", "hyper-v", "virtual hard disks", "itunes", "mobilesync", "apple", "minecraft", ".minecraft",
]}

SYSTEM_PROMPT = """You are a disk-space cleanup expert. You receive a JSON list of the largest items on a user's
computer (OS-critical locations were already excluded). Paths may be redacted: "~" is the user's home folder,
"<USER>" is the username, "<dir>"/"<file>" are hidden names. Identify items whose removal would reclaim space
without breaking the operating system, e.g.: installed programs/games the user may no longer need, caches,
temp files, old installers and downloads, build artifacts (node_modules, target, venvs), package-manager caches,
old backups and phone backups, VM/disk images, model files, Windows.old, Recycle Bin, crash dumps, log folders.
Do NOT recommend: personal documents/photos/videos unless they are clearly installers/archives/duplicates,
the user's whole home or Documents folder, or anything you cannot reasonably identify.
Prefer the most specific item (a single app or cache folder) over a broad parent folder.
Reply with ONLY a JSON array, no prose, each element:
{"id": <int>, "action": "delete" | "uninstall" | "clear_with_tool", "risk": "low" | "medium" | "high",
 "reason": "<short explanation, max 20 words; for uninstall/clear_with_tool name the tool or setting to use>"}
"delete" = safe to delete directly; "uninstall" = remove via the OS uninstaller / launcher;
"clear_with_tool" = use the owning app's cleanup command (e.g. "docker system prune", Disk Cleanup, npm cache clean).
Return an empty array if nothing qualifies."""


def scan(roots, min_bytes):
    """Walk the roots, returning {dir: [parent, size, files, newest_mtime, largest_child]} and big files."""
    protected = {os.path.normcase(os.path.join(r, p)) for r in roots for p in PROTECTED_DIRS}
    info, big_files, stack, last = {}, [], [(r, None) for r in roots], time.time()
    while stack:
        path, parent = stack.pop()
        rec = info[path] = [parent, 0, 0, 0.0, 0]
        if time.time() - last > 1:
            last = time.time()
            print(f"\r  scanned {len(info):,} folders...", end="", flush=True)
        try:
            entries = os.scandir(path)
        except OSError:
            continue
        with entries:
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    if e.is_dir(follow_symlinks=False):
                        if not attrs & REPARSE_POINT and os.path.normcase(e.path) not in protected:
                            stack.append((e.path, path))
                    elif e.is_file(follow_symlinks=False) and not attrs & CLOUD_ONLY \
                            and e.name.lower() not in PROTECTED_FILES:
                        size = st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
                        rec[1] += size
                        rec[2] += 1
                        rec[3] = max(rec[3], st.st_mtime)
                        if size >= min_bytes:
                            big_files.append((e.path, size, st.st_mtime))
                except OSError:
                    pass
    print(f"\r  scanned {len(info):,} folders.          ")
    for path in sorted(info, key=lambda p: p.count(os.sep), reverse=True):  # roll sizes up to parents
        _, size, files, mtime, _ = rec = info[path]
        if rec[0] in info:
            par = info[rec[0]]
            par[1] += size
            par[2] += files
            par[3] = max(par[3], mtime)
            par[4] = max(par[4], size)
    return info, big_files


def redact(path, is_file, strict):
    home, user = os.path.expanduser("~"), getpass.getuser()
    if os.path.normcase(path).startswith(os.path.normcase(home + os.sep)):
        path = "~" + path[len(home):]
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if strict:
        keep = [p if p.lower() in WELL_KNOWN or p.endswith(":") else "<dir>" for p in parts]
        if is_file:
            keep[-1] = "<file>" + os.path.splitext(parts[-1])[1]
        parts = keep
    out = "/".join(parts)
    return re.sub(re.escape(user), "<USER>", out, flags=re.I) if len(user) > 2 else out


def ask_llm(args, key, user_msg):
    if args.provider == "claude-code":
        # Runs through Claude Code (pip install claude-agent-sdk) using your existing Claude Code login - no API key.
        # tools=[] means the agent cannot read files or run commands: it only sees the redacted list, like any provider.
        import asyncio
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, CLINotFoundError, ResultMessage, query
        opts = ClaudeAgentOptions(system_prompt=SYSTEM_PROMPT, tools=[], setting_sources=[], mcp_servers={},
                                  strict_mcp_config=True, max_turns=1, model=args.model, cli_path=args.claude_path)

        async def run():
            result = None
            async for msg in query(prompt=user_msg, options=opts):  # let the stream finish so the SDK closes cleanly
                if isinstance(msg, ResultMessage):
                    result = msg
            return result
        login_hint = "If it says 'Not logged in', sign in once by running `claude` in a terminal and typing /login."
        try:
            result = asyncio.run(run())
        except CLINotFoundError:
            sys.exit("Claude Code not found. Install it or pass --claude-path to claude.exe.")
        except ClaudeSDKError as e:
            sys.exit(f"Claude Code error: {e}\n{login_hint}")
        if result is None or result.is_error:
            sys.exit(f"Claude Code error: {result and (result.result or result.errors)}\n{login_hint}")
        return result.result or ""

    if args.provider == "anthropic":
        import anthropic  # pip install anthropic
        client = anthropic.Anthropic(api_key=key or None, base_url=args.base_url or None)
        req = dict(model=args.model, max_tokens=16000, system=SYSTEM_PROMPT,
                   messages=[{"role": "user", "content": user_msg}])
        if args.model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"):
            # Server-side fallback: if a safety classifier declines, the API retries on a fallback model.
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **req)
        else:
            resp = client.messages.create(**req)
        if resp.stop_reason == "refusal":
            sys.exit("The model declined the request.")
        return "".join(b.text for b in resp.content if b.type == "text")

    body = {"model": args.model, "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                              {"role": "user", "content": user_msg}]}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(args.base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        sys.exit(f"LLM API error {e.code}: {e.read().decode(errors='replace')[:500]}")


def main():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):  # optional KEY=VALUE file; real environment variables take precedence
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and v.strip() and not k.startswith("#"):
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

    ap = argparse.ArgumentParser(description="AI-powered storage optimizer", epilog="See module docstring for setup.")
    ap.add_argument("--provider", choices=["openai", "anthropic", "claude-code"],
                    default=os.getenv("LLM_PROVIDER", "openai"),
                    help="'openai' = any OpenAI-compatible /chat/completions API (default); "
                         "'claude-code' = your Claude Code login via the Claude Agent SDK (no API key)")
    ap.add_argument("--model", default=os.getenv("LLM_MODEL"))
    ap.add_argument("--base-url", default=os.getenv("LLM_BASE_URL"))
    ap.add_argument("--claude-path", default=os.getenv("CLAUDE_CODE_PATH"),
                    help="claude-code provider: path to a specific claude executable (default: the SDK finds one)")
    ap.add_argument("--roots", nargs="+", help="folders/drives to scan (default: all fixed drives, or /)")
    ap.add_argument("--min-gb", type=float, default=0.5, help="ignore items smaller than this")
    ap.add_argument("--max-items", type=int, default=250, help="max items sent to the LLM")
    ap.add_argument("--privacy", choices=["balanced", "strict"], default="balanced")
    ap.add_argument("--show-payload", action="store_true", help="print the redacted item list and exit")
    ap.add_argument("--demo", action="store_true",
                    help="print the exact system + user messages the LLM would receive, then exit without sending")
    ap.add_argument("--yes", action="store_true", help="don't ask before sending to the LLM")
    ap.add_argument("--clean", action="store_true", help="interactively move 'delete' items to the Recycle Bin")
    args = ap.parse_args()
    if args.provider != "claude-code":  # claude-code uses your Claude Code default model unless --model is given
        args.model = args.model or ("claude-opus-5-5" if args.provider == "anthropic" else "gpt-5")
    if args.provider == "openai" and not args.base_url:
        args.base_url = "https://api.openai.com/v1"

    if args.roots:
        roots = [os.path.abspath(r) for r in args.roots]
    elif os.name == "nt":
        import ctypes
        roots = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 if ctypes.windll.kernel32.GetDriveTypeW(f"{d}:\\") == 3]  # 3 = fixed local disk
    else:
        roots = ["/"]

    min_bytes = int(args.min_gb * GB)
    print(f"Scanning {', '.join(roots)} (this can take a few minutes)...")
    info, big_files = scan(roots, min_bytes)

    # Candidates: big folders that aren't just a wrapper around one dominant child, plus big single files.
    items = [(rec[1], path, rec[2], rec[3]) for path, rec in info.items()
             if rec[0] is not None and rec[1] >= min_bytes and rec[4] < 0.9 * rec[1]]
    items += [(size, path, 1, mtime) for path, size, mtime in big_files]
    items = sorted(items, reverse=True)[:args.max_items]
    if not items:
        sys.exit("Nothing above the size threshold was found.")

    now = time.time()
    payload = [{"id": i, "path": redact(path, path not in info, args.privacy == "strict"),
                "type": "dir" if path in info else "file",
                "size_gb": round(size / GB, 2), "files": files, "days_since_modified": int((now - mtime) / 86400)}
               for i, (size, path, files, mtime) in enumerate(items)]
    user_msg = json.dumps({"os": f"{platform.system()} {platform.release()}", "items": payload}, separators=(",", ":"))

    if args.show_payload:
        print(json.dumps(payload, indent=1))
        return

    host = "Claude Code (your Claude login)" if args.provider == "claude-code" \
        else "api.anthropic.com" if args.provider == "anthropic" and not args.base_url \
        else urllib.parse.urlparse(args.base_url).netloc
    model_name = args.model or "your Claude Code default model"
    if args.demo:  # the exact messages the LLM would receive; nothing is sent and no API key is needed
        print(f"\n=== DEMO: request that would be sent to {host} (provider={args.provider}, model={model_name}) ===\n"
              f"\n--- system message ---\n{SYSTEM_PROMPT}\n\n--- user message ({len(user_msg):,} bytes) ---\n{user_msg}\n"
              f"\n=== Nothing was sent. ===")
        return
    if args.base_url and args.base_url.startswith("http://") and not host.split(":")[0] in ("localhost", "127.0.0.1"):
        print("WARNING: base URL is not HTTPS - data and API key would be sent unencrypted.")
    if not args.yes and input(f"Send {len(payload)} redacted entries ({len(user_msg):,} bytes, privacy={args.privacy})"
                              f" to {host} ({model_name})? Use --demo to inspect. [y/N] ").lower() != "y":
        return

    key = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY" if args.provider == "anthropic" else "OPENAI_API_KEY")
    if not key and args.provider == "openai":
        key = getpass.getpass("API key (leave empty for local servers): ").strip()

    print("Asking the AI for recommendations...")
    reply = ask_llm(args, key, user_msg)
    try:
        recs = json.loads(reply[reply.find("["):reply.rfind("]") + 1])
    except ValueError:
        sys.exit(f"Could not parse the AI's reply:\n{reply[:1000]}")

    # Map IDs back to real paths locally; drop invalid IDs and items nested inside another recommendation.
    recs = [dict(r, path=items[r["id"]][1], size=items[r["id"]][0]) for r in recs
            if isinstance(r, dict) and isinstance(r.get("id"), int) and 0 <= r["id"] < len(items)]
    recs = [r for r in recs if not any(o is not r and os.path.normcase(r["path"]).startswith(
        os.path.normcase(o["path"].rstrip(os.sep) + os.sep)) for o in recs)]
    recs.sort(key=lambda r: r["size"], reverse=True)
    if not recs:
        print("The AI found nothing it considers safely removable.")
        return

    print(f"\n{'SIZE':>9}  {'RISK':6} {'ACTION':15} PATH\n{'':>9}  {'':6} {'':15} reason")
    for r in recs:
        print(f"{r['size'] / GB:8.2f}G  {r.get('risk', '?'):6} {r.get('action', '?'):15} {r['path']}\n"
              f"{'':>9}  {'':6} {'':15} {r.get('reason', '')}")
    total = sum(r["size"] for r in recs)
    low = sum(r["size"] for r in recs if r.get("risk") == "low")
    print(f"\nPotentially reclaimable: {total / GB:.1f} GB ({low / GB:.1f} GB low-risk)")
    with open("storage_report.json", "w", encoding="utf-8") as f:  # real paths stay on this machine
        json.dump(recs, f, indent=1)
    print("Full report saved to storage_report.json")

    if args.clean:
        try:
            from send2trash import send2trash
        except ImportError:
            sys.exit("--clean needs: pip install send2trash")
        for r in (r for r in recs if r.get("action") == "delete" and r.get("risk") != "high"):
            if input(f"Move to Recycle Bin: {r['path']} ({r['size'] / GB:.2f} GB)? [y/N] ").lower() == "y":
                try:
                    send2trash(r["path"])
                    print("  moved.")
                except OSError as e:
                    print(f"  failed: {e}")
        print("Note: items marked 'uninstall' / 'clear_with_tool' should be removed with the named tool.")


if __name__ == "__main__":
    main()
