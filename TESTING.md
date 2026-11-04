# Capture verification — 2026-10-04

## Very large folder — 2026-11-04

`python -m pytest -q`: **165 passed, 1 skipped** (the opt-in native credential test).

Checked on one real folder holding many projects: 550 chats (466 Codex threads including
child reviews, plus Cursor), 1,127 source files, on a machine with 15.8 GB of memory.
Each run used a temporary copy of the journal and left the project folder unchanged.

| | Before | After |
| --- | --- | --- |
| First capture | 13 min | about 5 min |
| Journal size | 2.37 GB | about 850 MB |
| Project resume, repeat | 13+ min, never finished | about 4 s |
| Repeat capture, nothing changed | 33 s | under 1 s |
| Search | over 6 min | 0.5–2.5 s |
| Search result size | 45K characters | at most about 24K |
| Free memory, lowest drop during capture | not measured | about 1.6 GB |

Accuracy against Codex's rollout files, read without Handoff's parser:

- No thread missing; 26,168 of 26,168 tool calls and all their outputs present.
- Requests in time order in all 339 chats with history (262 Codex, 77 Cursor).
- Tool calls under the right request: 25,727, plus 510 under a tagged message such as a
  question reply, which the comparison had to learn to count as a request. None elsewhere.
- No message repeated in the same chat at the same second.
- 37 messages don't match exactly: 32 automated Guardian reviews and 5 others, some
  containing secrets. Redaction is the likely reason; this was not confirmed for all 5.
- 9 credential-shaped strings returned, all with a two-character template as the password.

An earlier comparison reported this data as mostly correct while three chats were out of
order, because it checked presence and not order. A direct time-order check now covers it.
Upgrading a journal saved with wrong placement was also run: the same counts came back.

Not checked: whether that upgrade adds duplicate events (it added 1,321 new events), and
the quality of decision extraction on this folder, which listed 938 items, some of them
passing remarks. On Claude Code chats, `latest_request` can pick up a system notice
instead of the user's words. Figures are observed local runs, not a controlled benchmark.

## Codex SQLite history capture — 2026-11-01

`python -m pytest -q --tb=short`: **92 passed, 1 skipped**. The skipped check
is the opt-in native credential test. SQLite fixtures cover database-only chats,
extended Windows workspace paths, parent/child links, turn/item identities, commands,
file changes and tool results, nested secret redaction, unknown records, incremental
sync, source rewrites, JSONL overlap and newer tails, missing/corrupt schemas, metadata-only
threads, orphaned history, discovery failures, and transaction rollback/retry.

A database-only fixture also completes transfer into an isolated destination setup,
then launches a fresh configured MCP process and retrieves the captured exchanges and
decision evidence. Source databases remain unchanged in the fixture checks.

Real-source checks used disposable journals for eight local projects, recovering
16 database-listed chats and 7,858 normalized events. Three of those chats had no
available history items and were explicitly reported. Histories without state metadata
were also reported rather than assigned to a project by guesswork. No real project
exports, destination configuration, credentials, or paid provider calls were changed.

A full real-source capture of `autoc_stuff` also completed in a disposable journal:
466 thread identities, 526 source snapshots, 22,781 normalized events, and 46,298 source
records. The initial reconciliation took 485 seconds. After adding journal lookup
indexes, a second run captured the same counts in 102 seconds. These are observed
local runs, not a controlled performance benchmark. Both temporary journals were removed.
346 SQLite source entries had no history items; their metadata and coverage warnings
were retained, with JSONL content also captured wherever available. One scan gap
reported database histories lacking state metadata/project association.

Capture reads the latest versioned state/history databases as separate read snapshots.
Unavailable content cannot be reconstructed. Confirmed exact matches suppress normalized
JSONL duplicates; unconfirmed overlaps remain visible with a coverage note. Original
source rows remain archived. Existing JSONL annotations may become stale when normalized
events switch to SQLite identities; their original source revisions remain archived.
The implementation adds no runtime dependency and requires no reinstall for editable installs.

## Project-wide transfer (current)

`python -m pytest -q --tb=short`: **81 passed, 1 skipped**. All five runtime
modules also passed `compileall`. The skipped check is the opt-in native vault test.

An end-to-end fixture captures multiple chats and a parent/child relationship,
exports normalized and redacted source history, configures an isolated Cursor MCP
file while retaining an unrelated server, then launches the configured Python command
from another directory and retrieves project-wide context through real MCP stdio.
It repeats transfer after a source disappears and verifies retained history and the
unavailable coverage entry. Other checks cover malformed config preservation,
empty-history reporting, CLI cancellation/validation paths, destination-only selection,
all four setup paths, and a shared project-wide extraction budget with repeat caching.

No actual destination UI was launched; Codex/Claude configuration commands and TypeLLM
responses were mocked. No live conversations were sent to a provider or real user
destination settings changed. Destination consumption remains explicitly unverified.
The implementation reuses the existing CLI, journal, and integrations without new
dependencies. The older sections below document previous milestones.

## Numbered selection and oversized extraction

Regression run: `python -m pytest -q --tb=short` — **73 passed, 1 skipped**.
The skipped test is the opt-in native credential test; no real credentials were
read and no paid TypeLLM generation calls were made during this change.

New tests cover numbered selection, invalid input, cancellation, remembered choices,
switching, project isolation, deletion, child labels/main-first ordering, and the
ID-free extraction CLI with refresh and caching. Slice tests reconstruct every
character of long plain, escaped-control, Unicode, and assistant messages, enforce
the serialized size bound, and reject evidence outside text actually sent.
TypeLLM responses are mocked: live provider accuracy is not established by these tests.

The implementation reuses the existing CLI and SQLite journal with no new dependency.
Splitting preserves captured text but limits the context visible in each model call;
it does not guarantee exhaustive or perfectly interpreted decisions.

## TypeLLM secure setup and extraction

Final regression result: **60 passed, 1 skipped** (the opt-in native credential test).
That native test was run separately and passed.

Tests exercise hidden input, refusal to fall back to echoed input, validation before
saving, preserving the old key after failed validation, status/check/remove, HTTP
authentication/credit/rate-limit/server failures, network errors, invalid JSON, and
redirect refusal. Extraction tests exercise real annotation validation with mocked
TypeLLM responses, indirect acceptance with preceding-message evidence, invalid quotes,
repeat-run caching, call limits, redaction, and oversized-window reporting.

The actual Windows Credential Manager round-trip test passed separately using a
uniquely named dummy credential. It was read, replaced, and deleted; no existing user
credentials were read or modified. This test is opt-in via HANDOFF_TEST_CREDENTIALS=1.

No real TypeLLM API key was supplied, so authenticated provider validation/generation
has not been run. Setup performs a real `/v1/models` check when the user enters a key.
API request shapes were checked against https://typellm.ai/docs/api .

## Decision tracking and full-tool regression pass

Final run: `python -m pytest -q` — **46 passed**. All four runtime modules also
passed Python compilation checks.

The expanded suite exercises automatic goal/choice/correction/rejection/request
extraction, exact quote validation, cross-project evidence rejection, assistant-only
evidence rejection, stale annotations, correction/conflict links, pagination, and
scoped deletion. Questions, tentative language, quoted Markdown, code fences, and
conditional choices are not automatically promoted to accepted decisions.

Completion claims remain unverified and confined to the same exchange; finishing a
later request does not close an earlier one. Automatic extraction is deliberately
conservative English wording detection, not exhaustive semantic understanding.

Full CLI workflow exercised: sync, coverage, conversations, decisions, resume, read,
search, export, history, doctor, stale-state check, and deletion. Existing tests cover
prompted/Git-only handoffs and staged/unstaged changes. Setup/uninstall runs against
temporary Cursor/Muse configs and mocked Codex/Claude command runners, not user config.
Packaged-wheel execution, MCP stdio tools/prompts, invalid-evidence error IDs, recovery
after errors, source rewrites, partial writes, database rollback, redaction, and the
search fallback are tested. The doctor check was fixed to recognize all ten MCP tools.

Live local exercise: captured 14 Codex conversations in a disposable database,
extracted 13 entries for the selected root conversation (8 requests, 5 goals), and
verified every supporting quote against its source event. SQLite integrity returned
`ok`. The actual `python -m handoff decisions` CLI returned a three-item page with the
correct continuation offset. The disposable database was cleaned up afterward.

The Cursor CLI was invoked as a potential live receiver; `cursor-agent status`
reported `Not logged in`. No live receiving-model test was therefore completed.
This is a concrete remaining verification gap, not a claim that all possible tools,
storage versions, or model interpretations have been tested.

## Continuation milestone (subsequent verification)

31 tests pass, including the capture suite and new continuation checks:

- Chat C is selected explicitly; Chat A's offline requirement is retrieved with its reply.
- Multiple conversations or matching IDs across tools require a choice, never a newest-file guess.
- Tool call/result pairs crossing user-message boundaries remain together.
- Oversized exchanges request retrieval rather than returning shortened messages.
- Pagination and search reach a 620-exchange history; cross-project reads are rejected.
- Missing or nonmonotonic timestamps do not reorder records within a source file.
- CLI conversation listing/resume/search and MCP subprocess selection/read/search work.

A read-only live Codex check using a temporary database found 12 thread choices.
The selected root conversation had 33 inferred exchanges. Its newest exchange exceeded
the default budget and returned `read_required`; reading the first exchange returned
12 events, and contextual search returned a surrounding-exchange result. SQLite
integrity passed. Exchange counts are inferred, not claims about native turn counts.
No live destination-model continuation was tested.

`python -m pytest -q`: **26 passed**, including the packaged-wheel smoke test.
`python -m py_compile handoff.py handoff_sessions.py handoff_capture.py`: passed.

## Exercised

- Three conversations and repeat synchronization without duplicate records.
- Appends, rewritten histories, partial JSON repaired on the next run, and 1,200 messages.
- Codex native thread IDs, child threads sharing a root session ID, and multiple continuation files.
- Claude parent/child identities and explicit child selection.
- Cursor conversation enumeration, unrelated-workspace exclusion, and legacy JSON secret redaction.
- Unknown records, unreadable/deleted sources, source mutation during capture, and rollback after an injected database failure.
- Cross-project identity conflicts, ordered archive reconstruction, and scoped deletion.
- CLI synchronization, coverage, and multi-conversation export.
- Actual MCP stdio subprocess: initialize, tool discovery, capture, and resume.
- Wheel built without dependency downloads, extracted outside the checkout, and exercised through the CLI.

## Live verification

Read-only scan of local histories for this project into a temporary database:

- 10 distinct Codex thread IDs across 12 files, including child threads.
- 2,390 source records archived and 804 normalized events imported.
- No identity conflicts or normalization failures; SQLite integrity check returned `ok`.
- 11 candidate sources had no usable project association and were explicitly reported, not imported.
- A second scan added four source records and one event while this conversation was active.
  The unchanged-source regression tests separately verify zero duplicate additions.
- Initial sandbox access failures were distinguished from source failures by rerunning the read-only audit with approved access.

The temporary live-verification database was removed. Existing user history and the
normal Handoff journal were not changed by this audit.

## Accuracy against Codex's own files

Each real project was captured into a temporary copy of the journal, then compared with
the Codex rollout files, which were read separately without using Handoff's parser.

| Project | User messages | Tool calls under the right request | Resume size | First / repeat |
| --- | --- | --- | --- | --- |
| Small, 4 Codex threads | 154 of 154 | 1,244 of 1,244 | ~3,250 tokens | 12.7s / 2.5s |
| Codex and Cursor history | 55 of 55 | 1,416 of 1,416 | ~3,300 tokens | 20.2s / 8.3s |
| This repository's main chat | 56 of 56 | 354 of 354 | ~3,830 tokens | 12.3s / 5.2s |

Each project also had every Codex thread present and no duplicated messages. The only
credential-shaped string returned was a `ghp_xxxxxxxx` placeholder.

The real server command also ran end to end against a copy of an older journal
(initialize, tool list, resume): it answered in about a second, upgraded the journal
during the first resume, and did not grow it.

The second project's journal grew by 52 MB, mostly from its Cursor archive and tool
metadata; this later fell to about 25 MB. Decision extraction can still pick up pasted text that was not the user's own decision.

## Limits of this verification

Live matching history was Codex history. Cursor, Claude, and Muse parsing has fixture
coverage; a live cross-tool continuation inside those applications was not performed.
This validates the capture milestone, not seamless context restoration or decision
extraction. Cloud-only history, attachment contents, other worktrees, and unknown
storage schemas remain outside automatic capture coverage.

The live audit also corrected the earlier claim of “11 conversations”: files were
being counted as conversations, and root session IDs were incorrectly used for child
thread identity. Native thread IDs and source-file counts are now reported separately.
