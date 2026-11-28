# Threadline

Open any coding agent in a project and continue as if you never left. Threadline keeps one
local, searchable history of your coding agents' chats, so the next agent can ask "where did
we stop?" and get a short, accurate answer with your latest request first. No choosing chats
or IDs. Formerly called Handoff; the `handoff` command still works.

Agents it reads: Claude Code, Codex, Cursor, Zed, GitHub Copilot (VS Code and CLI), Gemini CLI,
Cline, Roo Code, Kilo Code (extension and CLI), OpenCode and Muse Code. Cline's newest session
store and VS Code's newer `.jsonl` chat files are not read yet; such chats are listed with a note.

## Install

macOS and Linux:

```bash
curl -LsSf https://threadline-409ee.web.app/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://threadline-409ee.web.app/install.ps1 | iex"
```

The installer gets [uv](https://docs.astral.sh/uv/) if it is missing, installs Threadline, and
runs `threadline setup`, which connects every coding agent it finds. Existing agent settings are
backed up once with a `.handoff.bak` suffix, and other MCP servers are kept. Set
`THREADLINE_LITE=1` for a smaller install with words-only search. Check everything with
`threadline doctor`.

## What happens automatically

There is nothing to run. After setup, restart your agents once. Then:

- **Every new chat picks up where you left off.** Claude Code gets a short brief through a
  start-of-chat hook. Codex, Gemini CLI and OpenCode get a one-line note in their global
  instructions file telling them to call Threadline first. In other agents, ask "where did we stop?".
- **New chats are saved** whenever an agent asks Threadline anything, from every agent at once.
- **Secrets are removed** before anything is saved.

Agents get three tools: `resume` (where work stopped), `search_context` (find anything said
earlier) and `read_conversation` (the full messages of one chat).

## The page: `threadline open`

`threadline open` opens a page that shows where all your work stands, in your own words:

- **Your day.** The page opens on a recap of your last day of work across every project and agent,
  then each project with its stretches of work, each one titled.
- **Where you stopped**, for each project. This summary is saved and handed to the next agent that
  resumes the project.
- **The timeline.** Your work split into stretches wherever you paused for 45 minutes, each with a
  title and one line ("You and Codex added logging to the retry loop").
- **The thread.** Every chat drawn as one line that changes color where the work passed to another
  agent, each chat with a card: a title like a commit message and one line on what happened.
- **Search** across every chat, with the passage that answers your question.

Your history stays on your computer: the page reads it from a small local server that answers only
that page, with a one-time token.

### How the page writes

The recaps, summaries and cards come from Gemini through Firebase AI Logic in hybrid mode: Gemini
Nano in Chrome on your computer when it's available, otherwise a Gemini cloud model. To use Nano,
click "Download Gemini Nano" in the top-right tag once; it needs desktop Chrome, a GPU with more
than 4 GB of memory or 16 GB of RAM, and room for the download. Answers are locked to a fixed JSON
shape. Messages that look like they hold a password or key are read only by Nano; without Nano
they are left out, and the page says so.

## Search quality

Search combines word matching (SQLite FTS5) with search by meaning (a small local model,
all-MiniLM-L6-v2, 90 MB, downloaded once). Set `HANDOFF_MEANING=0` to turn meaning search off.

- **It returns the answer, not a pointer:** the passage inside a long message that answers the
  question, so the agent rarely has to open the chat.
- **The two searches are merged with reciprocal rank fusion**, so a message near the top of both lists
  wins, and words found in more than 0.5% of messages don't count as matches.
- **Results fit a fixed budget:** repeated messages appear once, and further matches are listed by name.
- **Compressed Codex history (`.jsonl.zst`) is read**, and the journal is compressed with zstd
  dictionaries trained on its own rows.

[benchmark/RESULTS.md](benchmark/RESULTS.md) compares Threadline with four other history tools
on real questions from real chats: accuracy, tokens per answer, disk, build time and memory,
including where it loses (searches are slower).

## Commands

```text
threadline                        Show whether it is working and what this folder holds
threadline setup                  Connect detected agents and turn on start-of-chat pickup
threadline open                   Open the page: your day, where you stopped, every chat
threadline doctor                 Check everything and say how to fix each problem
threadline uninstall              Remove Threadline's entries, hook and notes (history is kept)
```

History lives in `~/.handoff/handoff.db`. Original agent sessions are only read, never changed.
`threadline delete --project PATH --yes` deletes one project's history.

## Limits

Only local history is read: cloud-only chats, other worktrees and attachment contents are not.
Chats belong to the exact folder the agent was opened in. Redaction is best effort, not a
guarantee that every credential format is recognized. Agents change their storage formats;
`threadline doctor` reports a broken source, and the others keep working.

## Development

```bash
python -m pip install -e ".[meaning]"
```

Python 3.9+. MIT license.
