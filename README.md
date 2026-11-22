# Threadline

Continue any project in any coding agent. Threadline keeps one local, searchable history of
your Codex, Cursor, Claude Code and Muse Code chats, so the next agent (or the next computer)
can ask "where did we stop?" and get a short, evidence-linked answer. Formerly called Handoff;
the `handoff` command still works.

It imports matching local conversations, preserves redacted source records in SQLite,
and gives the next agent a small evidence-linked working context through MCP.

## Install

macOS and Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/temic137/handoff/main/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/temic137/handoff/main/install.ps1 | iex"
```

The installer gets [uv](https://docs.astral.sh/uv/) if it is missing, installs Threadline with
search by meaning, and runs `threadline setup`, which registers Threadline with every coding agent it
finds (Claude Code, Codex, Cursor, Muse Code). Existing agent settings are backed up once with a
`.handoff.bak` suffix, and unrelated MCP servers are kept. Set `THREADLINE_LITE=1` for a smaller
install with words-only search. Check everything with `threadline doctor`.

If you already use uv: `uv tool install "threadline[meaning] @ git+https://github.com/temic137/handoff"`,
then `threadline setup`.

### Working on Threadline itself

`python -m pip install -e ".[meaning]"` from a clone uses changes in the folder without
reinstalling. Restart any agent that already has Threadline running so it loads the new code.

## Use

From the project folder, run:

```powershell
python -m handoff
```

Choose the destination agent, **not a conversation**. Or use one explicit command:

```powershell
python -m handoff transfer --to cursor
```

Destinations: `cursor`, `claude`, `codex`, `muse`. Add `--project PATH` if needed.
Transfer synchronizes all accessible exact-workspace histories, preserves their
identities/relationships, exports the captured journal, and configures only the chosen
destination's MCP entry. Existing unrelated settings are preserved; JSON settings are
backed up. No TypeLLM key or paid extraction is needed for transfer.

The bundle includes `HANDOFF.md`, `.handoff/context.json`, `conversations.json`,
`events.jsonl`, `transcript.md`, `decisions.json`, `coverage.json`, and redacted raw
record/snapshot/state exports. Previously archived chats remain included even if their
source is no longer available. These files can contain sensitive project information;
do not commit or share them without reviewing them.

Open the same project in the destination (reload its MCP configuration if needed) and type:

```text
Continue using handoff.
```

The MCP server asks agents to call `resume`. This synchronizes all conversations
matching the exact workspace path and returns a token-budgeted context with evidence
IDs and a coverage report. Default resume puts the latest request first, then how the
work ended, the project inventory, project-wide decision evidence and retrieval pointers.
It stays within its token budget; `read_conversation` returns full history on demand.
It ignores old remembered chat selections. Agents can call `capture` to synchronize, `coverage` to
inspect the latest report, `search` for normalized history, and `evidence` for full
normalized events. These tools do not recreate native chats in the destination.
`.handoff/transfer.json` distinguishes bundle preparation from integration setup.
Destination loading is **not verified** merely because configuration succeeded.
If the destination does not call resume, paste `.handoff/CONTINUE.txt` into its chat.
Cloud-only history, other worktrees, attachment files, and hidden model state are not
automatically transferred. Coverage reports known gaps; discovery cannot prove that
no inaccessible chat exists. No chats from unrelated workspace paths are imported.

Capture without creating a handoff file:

```powershell
python -m handoff sync --project C:\path\to\project
python -m handoff coverage --project C:\path\to\project
```

Inspect project context (optional advanced single-conversation retrieval is also supported):

```powershell
python -m handoff conversations --project C:\path\to\project --refresh
python -m handoff resume --project C:\path\to\project
python -m handoff read --project C:\path\to\project --source codex --session THREAD_ID --start 0 --limit 5
python -m handoff search --project C:\path\to\project --query "offline requirement"
```

MCP equivalents are `conversations`, `resume`, `read_conversation`, and `search_context`.
Conversation lists show source, title (explicitly marked when inferred), activity, and
parent IDs when recorded. CLI retrieval uses the existing journal unless `--refresh`
is supplied; MCP resume refreshes by default.

Explicit `resume --source TOOL --session ID` returns a contiguous suffix from one chat;
default project resume previews recent whole exchanges across chats,
without truncating message text. Boundaries are inferred from user messages; linked
tool calls/results crossing boundaries are kept together. This is not native turn
reconstruction. Source order within each file is retained even with missing timestamps.
The context budget uses a character-based token estimate, not an exact tokenizer.
If even the latest exchange exceeds it, `read_required` points to that full exchange.
`read_conversation` paginates full exchanges (a single exchange can be large).
`search_context` ignores filler words such as "what" and "did", ranks messages by how many of
the remaining words they contain and then by relevance, and returns up to three matches per chat.
Each match comes with the request it answered and that exchange's final reply; a long message is
shown from the passage that matches best. Copies of a chat inside child tasks, such as automatic
reviews, rank after the main chats. Tool output is searched only when messages give too few matches.

#### Search by meaning (optional)

Word search misses answers written in other words ("database client" against "DBeaver").
Installing the optional extra adds meaning search, which runs alongside word search:

```bash
pip install "handoff[meaning]"
```

It downloads the all-MiniLM-L6-v2 model (90 MB) once into `~/.handoff/models`. Each message's
first ~3,000 characters are stored as vectors in the journal, about 60 MB for 30,000 messages. The
MCP server indexes in the background, newest messages first, so recent work is searchable
within minutes; a large history (about 30,000 messages) takes roughly 35 minutes once, using
half the processor cores, and resumes where it stopped. `handoff index --project DIR` builds it
in the foreground with progress. Results then alternate between the best meaning match and the
best word match. Set `HANDOFF_MEANING=0` to turn it off. Child tasks such as reviews are not indexed.
No goal or accepted decision is inferred from a message such as “continue.”

### Large folders with many projects

Chats belong to the exact folder the agent was opened in. If your chats were recorded
at a parent folder, name it even when working in a subfolder, because resuming in the
subfolder alone finds little:

```text
Use Handoff's resume tool on C:\path\to\parent, then search it for my-subproject
and tell me where we left off.
```

The first capture of a very large folder takes minutes; one folder with 550 chats took
about 5 minutes and 850 MB. After that, resume takes seconds. `search_context` lists
every matching chat first, then the best passages, with long content shortened;
`read_conversation` returns the full messages.

To keep the journal small, messages are always stored whole, but tool output keeps only
its first 6,000 and last 2,000 characters, with `metadata.source_path` pointing to the
original. Source records over 2,000 characters keep a checksum instead of a copy, so a
change is still detected but that record can't be rebuilt from the journal. Cursor's
binary tool data is not stored.

## Decision evidence

### TypeLLM setup (Windows)

Run in a regular PowerShell terminal:

```powershell
python -m handoff configure typellm
```

Paste your key at the hidden prompt. Setup validates it using TypeLLM's `/v1/models`
endpoint, then stores it in Windows Credential Manager for your Windows user. No key
is written into this project, command arguments, or configuration files. An invalid key
does not replace a previously saved key. Environment variables are not required or used.
No terminal/app restart is needed. Check or remove it with:

```powershell
python -m handoff configure typellm --check
python -m handoff configure typellm --status
python -m handoff configure typellm --remove
```

Saving a key does not automatically send conversation history. Explicit extraction:

```powershell
python -m handoff extract
python -m handoff decisions
```

Run these from your project directory (or add `--project C:\path\to\project`).
Extraction refreshes local capture and processes **all project chats**, including
child-agent chats, with one shared call budget. No chat picker appears by default.
Per-chat status distinguishes no eligible user exchanges from successful processing.
This is optional paid interpretation, not the transfer command. Advanced users can
restrict extraction with `--source codex --session THREAD_ID` or `--choose`.
Use `--refresh` with explicit IDs to update capture, too.

Extraction sends redacted user/assistant text from the project conversations to the
hosted TypeLLM API and consumes account credits. Secret redaction is best effort;
project information remains in the text. Tool outputs are not sent by this extractor.
Default maximum is three generation calls per invocation; use `--max-calls N` (1–50).
Repeat the command to process remaining exchanges. Successful unchanged windows are
cached in the journal and are not resent. Each window contains an exchange and up to
two preceding exchanges, with at most three decision interpretations. Oversized windows
are split into overlapping, size-bounded text slices with message IDs and redacted-text
offsets. All current user/assistant text is covered as you continue running extraction;
`remaining` counts pending windows, and `split_windows` reports the split-window total.
Each slice has only nearby context, so distant references may remain unresolved.
This is bounded extraction, not
a claim of exhaustive history understanding. Quotes and IDs are checked before saving;
results remain labelled interpretations and include TypeLLM provenance.

API contract: https://typellm.ai/docs/api . Connection errors, rejected keys, exhausted
credits, and rate limits produce short errors without printing server bodies or keys.
No automatic retry of billable generation calls is performed.

Resume automatically includes a project-wide paginated decision preview (or one chat
when explicitly filtered).
To read the full paginated ledger (or decisions from all imported project chats):

```powershell
python -m handoff decisions --project C:\path\to\project --refresh --limit 20
```

This local version recognizes explicit English wording for goals, choices, corrections,
rejected approaches, and requests. Each entry quotes a source message and retains its
event ID and conversation identity. Quoted Markdown, fenced code, questions about choices,
and tentative wording are excluded from these automatic rules. Rules are conservative,
not exhaustive language understanding; review the evidence for any important conclusion.
Requests remain `completion_unknown` unless an assistant completion claim occurs in the
same exchange, which is labelled `completion_claimed_unverified`, not independently verified.

The receiving agent can use MCP `record_decision` for richer interpretations with exact
supporting quotes. Handoff validates quote existence and conversation/project ownership;
semantic interpretation remains labelled `agent_interpretation`. Corrections can reference
earlier entries, and conflicts can reference other conversations. Cross-conversation
supersession is rejected. Source changes mark unsupported annotations `stale_evidence`.
Automatic entries are rebuilt from current history and do not accumulate duplicate rows.

MCP `decisions` supports source/session filters and pagination. A selected-conversation
Markdown handoff includes decision evidence; detailed export also writes
`.handoff/decisions.json`. Project/session deletion removes corresponding annotations.

The report lists source/session identities, source paths, metadata supplied by the
source, record counts, redactions, unchanged/imported/unavailable status, and gaps.
Conversation totals count distinct native thread IDs, not files. A Codex thread can
span several continuation files; its child threads retain separate native IDs and
parent metadata. Source-file details remain individually visible in the report.
Conflicting identities across projects are archived and flagged rather than allowed
to replace another project's normalized history.
Synchronization inserts only new source-record revisions and updates normalized
events when sources change. It compares each source's size and modified time and skips
unchanged Codex and Cursor sources, so repeat captures are quick. A source that changed
is read again from the start; this is not byte-offset incremental reading.

For tools without MCP—or as a portable fallback—run this inside the project:

```powershell
threadline
```

This writes one visible `HANDOFF.md`. Detailed files are opt-in:

```powershell
threadline export
```

That additionally writes `.handoff/transcript.md`, `.handoff/events.jsonl`, and
`.handoff/coverage.json` for all conversations captured in that run. The main
HANDOFF.md asks for selection when multiple conversations match; exports still include all.

## Commands

```text
threadline setup                         Configure detected agents
threadline doctor                        Check runtime, history, journal, FTS5 and MCP
threadline uninstall                     Remove only Threadline's MCP entries
threadline                               Capture and write HANDOFF.md
threadline export                        Also export Markdown and JSONL details
threadline history                       List journaled projects
threadline delete --project PATH --yes   Delete one project's journal history
threadline compact                       Shrink the journal file (close agent apps first)
threadline --check HANDOFF.md            Detect stale repository state
threadline --source cursor               Force a source adapter
threadline --session ID                  Force a session
```

Uninstalling MCP configuration does not delete the journal or original agent sessions.

## Context quality

- Redacted source payloads up to 2,000 characters and their revisions remain in
  `~/.handoff/handoff.db`, compressed, and readable through the `capture_records` view,
  keyed by project, source, session, location, record key, and payload checksum. Each
  project/source/session/location is stored once in `capture_sources` and referred to by number. Larger records keep only their size and
  checksum. JSON formatting is not retained. Malformed JSON lines are archived as text.
  Unknown fields/types survive even when not normalized.
- Message text, metadata and archived records are compressed with zstd, using dictionaries
  trained on the journal's own rows (`zstd_dicts`). The `events` view reads them as text, and
  writes to it are compressed. `handoff compact` retrains the dictionaries and recompresses.
- `capture_snapshots` stores ordered record/checksum manifests so captured revisions
  can be reconstructed. Event-to-source links prevent rewriting one continuation
  file from deleting events captured from another.
- `resume(project, source, session_id, token_budget=4000)` returns recent complete
  exchanges and explicit omission counts, with retrieval instructions for earlier history.
- SQLite FTS5 powers local full-text search; ordinary `LIKE` search is the fallback.
- Repeated Cursor tool lifecycle updates are collapsed by tool-call ID.
- Git commit, branch, status, diff, and a state fingerprint travel with file handoffs.

Embeddings are intentionally not included: they add storage and dependencies but do
not replace the source text. Add hybrid vector search only after real retrieval tests
show FTS5 misses important context.

## Privacy and limits

Default capture and retrieval use no hosted service. Optional explicit TypeLLM extraction
sends selected redacted message text to its hosted API. Common API keys, bearer tokens, GitHub tokens,
AWS access keys, and private-key blocks are redacted before journal or export writes.
Source histories are opened read-only. The archive contains the records exposed by
local sources, not inaccessible model state. Redaction is best effort, not a guarantee
that every possible credential format is recognized.

Coverage is limited to known local storage locations. Codex active/archived JSONL
sessions and the latest versioned local `state_*.sqlite` / `thread_history_*.sqlite`
databases are scanned, along with Claude/Muse JSONL and Cursor workspace databases.
Codex SQLite capture keeps thread metadata, native turns/items, spawn relationships,
attachment references, projection cursors, and realtime rows in the redacted source
archive. Messages, commands, file changes and supported tool activity are normalized
for retrieval. Unknown item types and realtime/attachment rows remain archive-only.
Source databases are opened read-only; Windows extended workspace paths are normalized.

When JSONL overlaps SQLite, normalized events are omitted from the JSONL view only
when both the recorded projection boundary and an exact normalized match support it.
Newer/unmatched JSONL events and all source records remain captured. Unconfirmed
overlap is retained and explicitly reported, so duplicate tool activity can remain.
A compressed rollout merges into the database's messages only when most of those
messages sit at matching lines in that file, whichever file the database names; other
files stay separate, in time order. The same words in a later file count as a new message.
Database-listed threads with no history rows and history threads without project
metadata are reported as gaps; the tool cannot reconstruct content absent from both
storage formats. Separate read transactions are used for the state/history databases;
a later sync can pick up changes made by Codex during capture.
Current supported Cursor databases retain individual composer IDs, including child
conversations. Legacy Cursor collections are explicitly flagged when chat boundaries
cannot be recovered. Cloud-only chats, other worktrees, and attachment file contents
are not automatically captured. Workspaces are not merged by folder-name similarity.
Branch/fork metadata is preserved when supplied, not inferred from today's checkout.
Unreadable sources and unknown project associations are reported. Previously captured
sources that disappear remain archived and are reported unavailable.

General model-driven decision extraction, verification tied to historical workspace
snapshots, and automated destination launching are not implemented. Retrieval exposes evidence; it does not
guarantee that a receiving model will correctly interpret every requirement.

Harness storage schemas can change. `handoff doctor` reports missing sources and the
adapters fall back independently, so one broken source does not disable the others.

## Development

Runtime dependencies: none. Python 3.9+ and Git are recommended.

```powershell
python -m pytest -q
```
