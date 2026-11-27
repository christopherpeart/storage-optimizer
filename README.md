# AI Storage Optimizer

A single-file Python tool that scans your drives, finds the largest things that are **safe to remove**
(games, unused apps, caches, crash dumps, old installers, build artifacts, VM images...), and asks an
LLM of your choice which ones you can get rid of and how.

OS-critical locations (for example `C:\Windows`, System32, `pagefile.sys`, `hiberfil.sys`) are never
scanned or sent to the AI.

## Features

- **Works with any LLM.** Use any OpenAI-compatible API (OpenAI, OpenRouter, Groq, Gemini, Mistral,
  Ollama, LM Studio...) or Claude through the official Anthropic SDK.
- **Privacy first.** Only a short, redacted list of large items is shared. See [Privacy](#privacy).
- **Advice you can act on.** Each recommendation says whether to `delete` the item, `uninstall` it,
  or clear it with the owning app (`clear_with_tool`, e.g. `docker system prune`), and gives a risk
  level and a reason.
- **Optional cleanup.** `--clean` goes through the low/medium-risk `delete` items one at a time and
  moves each one you approve to the Recycle Bin.
- **No required dependencies** for OpenAI-compatible providers. Only the standard library is used.

## Requirements

- Python 3.9+
- For Claude: `pip install anthropic`
- For `--clean`: `pip install send2trash`

## Setup

1. Copy the template `env.txt` to `.env` (next to the script):

   ```sh
   copy env.txt .env      # Windows
   cp env.txt .env        # macOS / Linux
   ```

2. Fill in your provider and key in `.env`:

   ```ini
   LLM_PROVIDER=anthropic
   LLM_API_KEY=sk-ant-...
   LLM_MODEL=
   LLM_BASE_URL=
   ```

   You can also set these as ordinary environment variables, which override `.env`.
   `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are also recognised.

`.env` is git-ignored, so your real key is never committed. `env.txt` is only the template.
Don't put a real key in it.

## Usage

```sh
# Check exactly what would be sent to the AI. Nothing is sent.
python storage_optimizer.py --show-payload

# Run with the settings from .env
python storage_optimizer.py

# Examples with explicit providers
python storage_optimizer.py --provider anthropic
python storage_optimizer.py --model gpt-5 --base-url https://api.openai.com/v1
python storage_optimizer.py --model llama3.1 --base-url http://localhost:11434/v1   # local Ollama

# Scan specific drives, hide more names, then clean up interactively
python storage_optimizer.py --roots C:\ D:\ --privacy strict --clean
```

| Option | Default | Description |
|---|---|---|
| `--provider` | `openai` | `openai` (any OpenAI-compatible API) or `anthropic` |
| `--model` | `gpt-5` / `claude-opus-5-5` | Model name |
| `--base-url` | provider default | API endpoint |
| `--roots` | all fixed drives (or `/`) | Folders or drives to scan |
| `--min-gb` | `0.5` | Ignore items smaller than this |
| `--max-items` | `250` | Maximum number of items sent to the AI |
| `--privacy` | `balanced` | `balanced` or `strict` (see below) |
| `--show-payload` | | Print the redacted payload and exit without contacting the AI |
| `--yes` | | Skip the confirmation before sending |
| `--clean` | | Interactively move `delete` recommendations to the Recycle Bin |

The results print as a table and are also saved to `storage_report.json`. That file contains real
paths, is kept on your machine only, and is git-ignored.

## How it works

1. **Scan.** Walks every fixed drive, skipping protected OS folders, symlinks and junctions (so
   nothing is counted twice) and cloud-only OneDrive/Dropbox placeholder files.
2. **Pick candidates.** Keeps folders above `--min-gb` plus large single files. A folder is skipped
   when one subfolder holds 90% or more of it, so the item that actually uses the space is shown.
3. **Ask the AI.** Sends the redacted list with numeric IDs. The AI replies with JSON
   recommendations that refer to those IDs.
4. **Map back locally.** IDs are turned back into real paths on your machine. Invalid IDs are
   dropped, as is any recommendation nested inside another one, so totals aren't counted twice.

## Privacy

What is sent for each item: an ID, a redacted path, the type (file or folder), its size, file count,
and days since it was last modified. That's all.

- **Never sent:** file contents, small files, your API key in the payload, or anything from
  protected system folders.
- **`balanced` (default):** your home folder becomes `~` and your username becomes `<USER>`.
- **`strict`:** every folder name that isn't a well-known generic name (AppData, Temp,
  node_modules, Steam, DXCache...) becomes `<dir>`. Files keep only their extension.
- **You stay in control:** the tool asks before sending anything, and warns if the endpoint isn't
  HTTPS. Use `--show-payload` to check the exact data first.
- **Your key** comes from `.env` or the environment, never from the command line, so it doesn't end
  up in your shell history.

## Disclaimer

The AI's recommendations are suggestions. Read them before removing anything. Uninstall programs
with their uninstaller rather than deleting their folders. `--clean` only uses the Recycle Bin, so
anything it removes can be restored until you empty the bin.
