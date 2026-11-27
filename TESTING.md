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

No actual destination UI was launched; Codex/Claude configuration commands were mocked. No live conversations were sent to a provider or real user
destination settings changed. Destination consumption remains explicitly unverified.
The implementation reuses the existing CLI, journal, and integrations without new
dependencies. The older sections below document previous milestones.

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

## Search accuracy — 2026-11-07

Questions were made from exchanges drawn at random from the original Codex files of a
large folder (466 Codex chats), all from older compressed files, and each answer was read
from those files without Handoff. A script then asked `search_context` each question,
with the 24,000-character limit agents get, and checked whether the exact answer text
came back. "Short" is a few keywords, as agents usually search.

| Question set | Old search: full / short | New search: full / short |
| --- | --- | --- |
| 18 questions used while changing the search | 0 / 0 | 13 / 16 |
| 11 unseen questions, written before scoring | 1 / 0 | 9 / 11 |

The old search found the right chat almost every time but then showed that
chat's newest message containing any query word, so filler words decided the result.
Answers missed by the new search are in chats where many exchanges share the question's
words; `read_conversation` is still needed there. Cursor and Claude Code history were
not part of this check, and no agent answers were scored.

## Meaning search — 2026-11-11

29 questions worded the way a person asks, not with the words used in the chat (for example
"Which details do we pull from the national ID slip now?"), from the same folder; answers
read from the Codex files without Handoff. Scored like the section above, on a copy of the
journal (29,150 indexed messages). "Four searches" adds three rewordings per question,
written once by a small model without seeing the answers, each run as its own search.

| Search | One search | Four searches |
| --- | --- | --- |
| Words only | 4 / 29 | 15 / 29 |
| Words and meaning (all-MiniLM-L6-v2) | 12 / 29 | 21 / 29 |

Other ways tried on the same questions and dropped:
- Blending scores (reciprocal rank fusion) found fewer answers than alternating the two lists.
- A cross-encoder reranker (ms-marco-MiniLM-L-6) found fewer answers and added seconds per search.
- A static model (potion-retrieval-32M) indexed in seconds but found 7 of 29.
- Ranking the same candidates with bigger or newer models:

| Model | One search | Four searches | Full index |
| --- | --- | --- | --- |
| all-MiniLM-L6-v2 | 10 | 19 | ~33 min |
| snowflake-arctic-embed-xs | 8 | 18 | ~65 min |
| granite-embedding-small-english-r2 | 8 | 21 | ~118 min |
| nomic-embed-text-v1.5 (8-bit) | 14 | 19 | ~204 min |
| EmbeddingGemma-300m (8-bit, ~100 candidates) | 15 | not run | ~260 min |

With rewordings, the differences are within chance for 29 questions, so the smallest and
fastest model stays. Search takes about 2.3 s on this journal against 1.7 s with words only;
the first search in a session takes about 8 s while the model and vectors load, which the
background indexer does in advance. Timings are from one Windows laptop processor.

### Duplicates, a match list and rerankers — 2026-11-14

A second set of 30 questions was written by Haiku from randomly chosen Codex exchanges that
had not been looked at while tuning; each answer text was checked to appear in the Codex file.

| Change | First 29 | New 30 |
| --- | --- | --- |
| Meaning search, 4 wordings | 21 | — |
| + same message stored twice shown once | 22 | 23 |
| + room kept for a list of further matches (main results only) | 21 | 23 |
| + Haiku opens up to 3 listed matches with read_conversation | 22 | 27 |
| jina-reranker-v1-tiny re-ordering the top 40 instead | 20 | 24 |

The reranker added about 3.3 s per search (7.1 s at worst) and lost a question on the first
set, so it was not adopted. Haiku's choices for both sets cost about $0.034.

Smaller search replies were tried to save tokens (the match list's share scaled with the limit):

| Reply limit (characters) | First 29: results / incl. match list | New 30: results / incl. match list |
| --- | --- | --- |
| 8,000 | 13 / 14 | 14 / 15 |
| 12,000 | 13 / 17 | 14 / 22 |
| 16,000 | 16 / 19 | 21 / 25 |
| 24,000 | 21 / 23 | 23 / 27 |

The 24,000 limit stays. Comparing the question with all 40,669 stored passages takes about 6 ms
of a 3–12 s search, so clustering the vectors (k-means style indexes) would not make it faster.

The time went into building the reply: each search read about 18 whole chats (some with ~5,500
events) to find the exchange around each match, and kept reading after its space was full.
Search now skips matches that cannot fit, keeps a small outline of up to 32 recently searched
chats until a chat changes (a per-chat change count kept by database triggers), and finds the
best passage in one pass. On 6 questions × 4 wordings the replies were identical to before
apart from the left-out count:

| | Before | After |
| --- | --- | --- |
| First search | 11–13 s | 3.2 s |
| Each further wording | 10–11 s | 2.2 s |
| Slowest | 13–15 s | 6.8 s |

### Ranking from memex's ideas — 2026-11-26

Word matches no longer count words found in more than 0.5% of stored messages (at least 100),
and the word and meaning lists are merged by reciprocal rank fusion instead of taking one result
from each in turn. On the 27 tuning questions and 22 new ones (`benchmark/questions2.json`),
Threadline's first chat was right 82 times in 98 searches, against 72 before and 71 for memex.
Full tables are in `benchmark/RESULTS.md`.

## Limits of this verification

Live matching history was Codex history. Cursor, Claude, and Muse parsing has fixture
coverage; a live cross-tool continuation inside those applications was not performed.
This validates the capture milestone, not seamless context restoration or decision
extraction. Cloud-only history, attachment contents, other worktrees, and unknown
storage schemas remain outside automatic capture coverage.

The live audit also corrected the earlier claim of “11 conversations”: files were
being counted as conversations, and root session IDs were incorrectly used for child
thread identity. Native thread IDs and source-file counts are now reported separately.
