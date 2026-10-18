"""The agent under test for Scenario S6 (cache and memory), built on LangGraph.

  types.py      Passage, SourceRef, UserCtx, CacheEntry, MemoryItem, Delivery
  embedding.py  Embedder protocol + HashEmbedder, a deterministic local embedding
  llm.py        StubLLM (answers by quoting its context verbatim: the worst-case leaker) and the
                optional RealLLM (never used for benchmark numbers)
  stores.py     the semantic cache and the long-term memory: in memory and in Postgres + pgvector
  retrieval.py  retrieval adapters: Setup A (persona-trimmed index) and Setup B (/query)
  guard.py      the access check the GUARDED mode runs, from Setup B's state
  threads.py    who owns which conversation thread (LangGraph Postgres checkpointer)
  graph.py      the LangGraph graph and the Agent that runs it, in NAIVE or GUARDED mode
  queries.py    the fixed, versioned query set (queries.yaml)
  s6/           the S6 driver: scenarios, scoring against the oracle, report

Nothing here decides access for Setup A or B: it only DELIVERS what the retrieval returned, what a
cache entry holds or what a memory item says. The point of S6 is whether it delivers to someone who
may not see it. See docs/FINDINGS.md, section S6.
"""
