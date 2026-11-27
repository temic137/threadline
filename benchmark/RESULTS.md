# Search benchmark: Threadline, memex, cass, recall-cli and ccrider

## Latest run (2026-11-27): five tools, both sets, 98 searches each

Same 49 questions, each asked as a question and as keywords. cass's index (words and meaning) was brought
up to date first. recall-cli ran in its default hybrid mode with its local Ollama model (`qwen3-embedding:0.6b`).
ccrider has only word search.

| Tool | First | Top 5 | Answer shown | Median time | Index on disk | Index build |
|---|---|---|---|---|---|---|
| Threadline | **81** | 93 | **80** | 2.1–2.8 s | 451 MB | built up by capture |
| memex | 71 | **95** | 54 | 0.5–0.6 s | 740 MB | 11 min 17 s |
| cass | 43 | 71 | 37 | 0.4–0.6 s | 2.1 GB | 26 s words, 44 min meaning |
| recall-cli | 42 | 46 | 11 | **0.1 s** | 42 MB (plus a 639 MB model) | 14 min 47 s |
| ccrider | 21 | 32 | 18 | 0.1–1.1 s | 177 MB | 40 s |

- **Compressed Codex history:** Codex now writes `.jsonl.zst` files (620 of 623 here). recall-cli and ccrider
  skip them, so each indexed only 21–23 of the 49 answer chats. A missed chat counts as a miss.
- **On the 23 questions recall-cli could read** (46 searches): recall-cli first 42, top 5 46, answer shown 11;
  Threadline first 38, top 5 43, answer shown 40; memex 30 / 45 / 24.
- **On the 21 questions ccrider could read** (42 searches): ccrider first 17, answer shown 10; Threadline 34 / 36.
- ccrider's search has no project filter, so the runner keeps sessions from the project folder and its subfolders,
  as for memex. Its natural-language searches mostly return nothing (word search needs every word).
- Threadline's set-2 scores are 1–2 lower than on 2026-11-26 (journal changed since: duplicate copies removed).


Rerun on 2026-11-26 (first run 2026-11-22) on one Mac (Apple Silicon, 8 cores), over the same chat history: Claude Code and Codex chats
for a work project and Threadline itself.

## Method

The questions and per-question results are not published: they quote real work chats. `sample.py`
makes a new set from your own history.

- `sample.py` picked 27 random request/answer pairs from real chats (3 old test chats were dropped). These were
  used while tuning Threadline's ranking.
- A second set of 22 (`questions2.json`, `sample.py 23`, unclear or repeated requests dropped) was written
  before any tool was run on it, to check that changes were not fitted to the first set.
- For each one I wrote a natural question and a short keyword version, plus a phrase from the answer (the "needle").
- `run.py` asks each tool both versions, restricted to the project, in its best mode:
  - Threadline: `search_context` exactly as an agent receives it over MCP (24,000-character limit).
  - memex: `--mode hybrid`.
  - cass: `--mode hybrid`, with its MiniLM meaning index built.
- Scores:
  - **First:** the tool's first chat is the one the answer came from.
  - **Top 5:** that chat is among its first five different chats.
  - A resumed chat that holds the same answer message (same text and time) counts as the right chat, for every
    tool. Before this, Threadline's first set had one question at rank 15 that was really a copy at rank 1.
  - **Answer shown:** the needle appears in the first 24,000 characters the tool returns, so an agent sees the
    answer without opening the chat.
- Time is the median wall time per search, including process start.

## Results

27 questions used while tuning:

| Tool | Wording | First | Top 5 | Answer shown | Median time |
|---|---|---|---|---|---|
| Threadline | question | **23** | **27** | **21** | 2.15 s |
| Threadline | keywords | **20** | **26** | **23** | 1.80 s |
| memex | question | 22 | 26 | 16 | 0.48 s |
| memex | keywords | **20** | **26** | 17 | 0.39 s |
| cass | question | 4 | 6 | 1 | **0.42 s** |
| cass | keywords | 19 | 24 | 16 | **0.37 s** |

22 questions not used while tuning:

| Tool | Wording | First | Top 5 | Answer shown | Median time |
|---|---|---|---|---|---|
| Threadline | question | **19** | **22** | **17** | 1.34 s |
| Threadline | keywords | **20** | **21** | **20** | 1.16 s |
| memex | question | 15 | **22** | 9 | 0.42 s |
| memex | keywords | 14 | **21** | 12 | 0.38 s |
| cass | question | 6 | 13 | 3 | **0.38 s** |
| cass | keywords | 11 | 13 | 10 | **0.35 s** |

cass's question scores dropped from the first run (9 to 4 first); its meaning index may be stale. It was not
rebuilt for this run.

### What changed in Threadline's ranking (from reading memex's code)

memex ranks with two ideas Threadline lacked. Each was tried alone and together; only the pair helped on both
sets (scored in-process, so faster than the table above):

| Ranking | Set 1 first (q / k) | Set 2 first (q / k) | Answer shown, both sets |
|---|---|---|---|
| Before: one result from each list in turn | 20 / 19 | 18 / 15 | 75 of 98 |
| Common words ignored when counting matches | 20 / 19 | 18 / 15 | 78 |
| Reciprocal rank fusion of the two lists | 21 / 19 | 18 / 18 | 80 |
| Both (adopted) | **23 / 20** | **19 / 20** | **81** |
| Both plus simple stemming | 22 / 20 | 19 / 19 | 80 |
| Newer messages first on ties | 20 / 19 | 18 / 15 | 75 |

- **Common words:** a word in more than 0.5% of stored messages (and at least 100) no longer counts towards
  "how many of the query's words matched", the same rule memex uses. bm25 still weighs it.
- **Reciprocal rank fusion:** each message scores 1/(61 + rank) in the word list and in the meaning list, added
  together, so a message near the top of both beats one at the top of only one.

## Footprint

| | Threadline | memex | cass |
|---|---|---|---|
| Disk | 451 MB | 740 MB | 2.1 GB |
| Building the index | not re-measured (built up over time by capture) | 11 min 17 s | 26 s words, plus 44 min meaning |
| Agents read | 4 | 19 | 34 |

## What it means

- **Finding the right chat: Threadline now leads.** On the tuning set it is level with memex (43 against 42
  first); on the untouched set it leads by 39 against 29 first (of 44 searches). cass is behind.
- **Giving the agent the answer: Threadline leads clearly** (81 of 98 against memex's 54 and cass's 30).
  Threadline returns the matching passage, while the others mostly return snippets or pointers.
- **Speed: Threadline is about 3–4 times slower** per search (1.2–2.2 s against about 0.4 s). Most of that is
  starting Python and opening the journal on each call. An MCP server that stays running pays this once.
- **Size: Threadline is the smallest.**

## Fairness notes

- memex names projects three different ways, so its `--project` filter missed chats. The runner searches
  everything and keeps rows from the project folder and its subfolders. This raised memex from 14 to 21 first.
- cass names long Codex chats `rollout-<time>-<chat id>_<part id>.jsonl.zst`. The runner takes the chat id. An
  earlier parsing mistake had cass at 5 first.
- cass's index build reported that a few oversized Codex files were skipped.
- The questions were written by the same person who built Threadline; the keyword column guards a little
  against wording bias.
- 27 and 22 questions are small. Differences of one or two are not meaningful.
