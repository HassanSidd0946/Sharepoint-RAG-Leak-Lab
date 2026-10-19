---
title: "How long does a revoked user keep access? Measuring permission revocation in RAG over SharePoint"
subtitle: "SharePoint, Azure AI Search and a revocation layer, measured against the same changes — and why a shared agent cache leaks even when retrieval is correct"
author: "Muhammad Hassan Siddiqui"
date: "{{meta.publish_date}}"
---

<!--
TEMPLATE RULES
- Every number is a {{token}} filled by `leaklab.replay writeup` from report.json and the
  replay bundles. Never type a number into this file. The fill step fails on any unknown or
  missing token.
- Durations: use the |dur filter (e.g. 2565 -> "42.8 min", 10 -> "10 s").
- Re-run the fill step after every new benchmark export.
-->

> **TL;DR**
> - After removing a user from an **Entra security group**, plain **SharePoint** kept serving the file for a median of **{{S1-entra.setup0.first_deny_s.median|dur}}** before the first denial ({{S1-entra.setup0.n}} runs). **Azure AI Search's SharePoint indexer** in its basic ACL configuration stopped after **{{S1-entra.setup_a_basic.first_deny_s.median|dur}}**. A small **revocation layer** stopped after **{{S1-entra.setup_b.first_deny_s.median|dur}}**.
> - For **direct grants** and **SharePoint-group** grants, SharePoint revoked in seconds ({{S1-direct.setup0.first_deny_s.median|dur}} and {{S1-spgroup.setup0.first_deny_s.median|dur}}) — slightly faster than our layer. The gap is specific to Entra-group changes.
> - An agent with a **shared answer cache and memory** delivered one user's content to another in **{{S6.naive.leaks}} of {{S6.naive.trials}}** attack trials. In **{{S6.naive.leaks_via_cache_memory}} of those {{S6.naive.leaks}}** the leaked content came from the agent's shared cache or memory, in {{S6.naive.leaks_via_retrieval}} from retrieval. A guarded agent leaked in **{{S6.guarded.leaks}} of {{S6.guarded.trials}}**, and still served allowed users.
>
> *Scope: one Microsoft 365 test tenant, {{run.documents}} synthetic documents, {{run.personas}} test users, {{meta.measurement_dates}}. Azure AI Search measured in its basic ACL configuration. Full limitations below; all code, journals and the report are public.*

![Three systems after one Entra-group removal]({{figures.timeline}})
*Figure 1. One real run ({{figures.timeline_bundle}}, the replicate closest to the median). Each tick is one read by the removed user: green = file served, red = denied, grey = no answer. Vertical line: the change confirmed in Entra.*

## Why this question matters

Every team building a Microsoft 365 copilot eventually hears the same question from the client's security team: *"If we remove someone's access, how fast does the copilot stop showing them the document?"*

Most answers assume the copilot inherits SharePoint's permissions, so it's "as safe as SharePoint." That hides two things worth measuring: **how fast permission changes actually take effect** — in SharePoint itself and in anything that copies its permissions into a search index — and **whether the layers added on top of retrieval** (answer caches, agent memory) respect permissions at all.

So I measured it.

## What I measured

Three systems, observed in the same polling loop, after the same permission change:

| System | What it is |
|---|---|
| **SharePoint** | Reading the file through Microsoft Graph as the user. The reference point for "what the platform enforces." |
| **Azure AI Search (basic ACL)** | Microsoft's SharePoint indexer with document-level permission ingestion ({{meta.search_api_version}}, preview), users and Entra groups, shortest supported indexer schedule ({{meta.indexer_schedule}}). |
| **Revocation layer** | A FastAPI service I built in front of a separate, unfiltered index over the same data source. It keeps its own permission state from Microsoft Graph deltas and SharePoint role assignments, checks every result, and fails closed when that state is stale. |

Each synthetic document contains a unique **canary string**. A leak means one thing only: a canary was returned to a user who, according to an independent model of the intended permissions (the *oracle*), should not see it at that moment. No LLM judges anything.

Scenarios: revoking access granted **directly**, through a **SharePoint group**, and through an **Entra security group** (S1); removing a **nested** Entra group (S2); **moving** a file into a restricted folder (S4); and three **agent cache/memory** attacks (S6).

## Result 1: revocation timing depends on how access was granted

![First denial after the change, every replicate]({{figures.dotplot}})
*Figure 2. Time from the confirmed change to the first denial. Every replicate is a dot; log scale. Runs that did not stop within {{run.poll_cap_s|dur}} are drawn at the cap.*

| Scenario | SharePoint | Azure AI Search (basic ACL) | Revocation layer |
|---|---|---|---|
| Entra group member removed (n={{S1-entra.setup0.n}}) | {{S1-entra.setup0.first_deny_s.median|dur}} ({{S1-entra.setup0.first_deny_s.min|dur}}–{{S1-entra.setup0.first_deny_s.max|dur}}){{S1-entra.setup0.dnc_note}} | {{S1-entra.setup_a_basic.first_deny_s.median|dur}} ({{S1-entra.setup_a_basic.first_deny_s.min|dur}}–{{S1-entra.setup_a_basic.first_deny_s.max|dur}}) | {{S1-entra.setup_b.first_deny_s.median|dur}} ({{S1-entra.setup_b.first_deny_s.min|dur}}–{{S1-entra.setup_b.first_deny_s.max|dur}}) |
| Nested Entra group removed (n={{S2.setup0.n}}) | {{S2.setup0.first_deny_s.median|dur}} ({{S2.setup0.first_deny_s.min|dur}}–{{S2.setup0.first_deny_s.max|dur}}) | {{S2.setup_a_basic.first_deny_s.median|dur}} | {{S2.setup_b.first_deny_s.median|dur}} |
| Direct grant removed (n={{S1-direct.setup0.n}}) | {{S1-direct.setup0.first_deny_s.median|dur}} | {{S1-direct.setup_a_basic.first_deny_s.median|dur}} | {{S1-direct.setup_b.first_deny_s.median|dur}} |
| SharePoint-group member removed (n={{S1-spgroup.setup0.n}}) | {{S1-spgroup.setup0.first_deny_s.median|dur}} | never served the documents (see Result 2) | {{S1-spgroup.setup_b.first_deny_s.median|dur}} |

*Median (min–max) time to first denial. Leak exposure — how long the file was still served at all, including intermittent reads after the first denial — is in the full report.*

Three things stand out:

1. **SharePoint is fast for direct and SharePoint-group changes, and slow for Entra-group changes.** After an Entra-group removal it kept serving the file for {{S1-entra.setup0.first_deny_s.median|dur}} (median), and in some runs flipped between serving and denying for a while after that. Microsoft documents that permission changes can take time to apply; this measures how long, in one tenant, for this kind of change.
2. **Azure AI Search behaves differently from SharePoint, not uniformly better or worse.** It evaluates Entra-group membership at query time, with a lag of a few minutes — far quicker than SharePoint itself for that case. Direct-grant changes wait for the next indexer run, so they took {{S1-direct.setup_a_basic.first_deny_s.median|dur}}.
3. **A layer that tracks the directory directly can follow intent within seconds** — in our runs, {{S1-entra.setup_b.first_deny_s.median|dur}} for Entra-group removals, bounded by its polling interval and Graph's delta lag. For direct grants it is slightly *slower* than SharePoint ({{S1-direct.setup_b.first_deny_s.median|dur}} vs {{S1-direct.setup0.first_deny_s.median|dur}}).

The point is not "SharePoint is broken." It is that **"as safe as SharePoint" includes SharePoint's propagation delays**, and those delays depend on how access was granted.

## Result 2: SharePoint groups in the basic configuration

In the basic ACL configuration, documents granted to a **SharePoint group** were indexed with the group recorded, but never served to its members. Those users were silently locked out of content they are entitled to.

Microsoft documents an additional configuration for SharePoint-group support. **I did not test it**, so this result is about the basic configuration only — not about what Azure AI Search can do.

## Result 3: the agent's cache and memory leak — even with correct retrieval

| Scenario | Naive agent + Azure AI Search retrieval | Naive agent + revocation-layer retrieval | Guarded agent + revocation layer |
|---|---|---|---|
| S6a: one user's cached answer reaches another | {{S6a.agent_naive_a.leaks}} of {{S6a.agent_naive_a.trials}} leaked | {{S6a.agent_naive_b.leaks}} of {{S6a.agent_naive_b.trials}} leaked | {{S6a.agent_guarded_b.leaks}} of {{S6a.agent_guarded_b.trials}} leaked |
| S6b: cached answer survives the user's revocation | {{S6b.agent_naive_a.leaks}} of {{S6b.agent_naive_a.trials}} | {{S6b.agent_naive_b.leaks}} of {{S6b.agent_naive_b.trials}} | {{S6b.agent_guarded_b.leaks}} of {{S6b.agent_guarded_b.trials}} |
| S6c: shared memory recalls another user's facts | {{S6c.agent_naive_a.leaks}} of {{S6c.agent_naive_a.trials}} | {{S6c.agent_naive_b.leaks}} of {{S6c.agent_naive_b.trials}} | {{S6c.agent_guarded_b.leaks}} of {{S6c.agent_guarded_b.trials}} |
| Positive control: allowed user is served | {{S6pc.agent_naive_a.served}} of {{S6pc.agent_naive_a.trials}} | {{S6pc.agent_naive_b.served}} of {{S6pc.agent_naive_b.trials}} | {{S6pc.agent_guarded_b.served}} of {{S6pc.agent_guarded_b.trials}} |

**The leaked content was delivered from the agent's shared cache and memory, not from retrieval:** {{S6.naive.leaks_via_cache_memory}} of the {{S6.naive.leaks}} leaked trials came from the shared cache or memory, {{S6.naive.leaks_via_retrieval}} from retrieval. Neither store carries permission information. Retrieval was not re-run on the cache-hit turns, so these trials do not show whether Azure AI Search would have returned the revoked document at that moment (its direct-grant changes wait for the next indexer run; see Result 1). Using Azure AI Search or the revocation layer for retrieval gave the same leak counts. I did not test turning the cache off.

The guarded agent stores the source documents of every cache entry and memory item, and on every hit re-checks that the **current** user may read **every** source, right now. Unknown means deny. It refused every attack candidate and still served allowed users — it doesn't achieve "security" by refusing everything.

Two honest qualifications: the guarded agent still *stores* those answers (it controls delivery, not storage), and the benchmark uses a stub LLM that quotes its sources verbatim — the worst case. A real model may paraphrase, but in a one-off illustration a real model also repeated the leaked reference code.

## What to do about it

1. **Test revocation before go-live.** Remove a test user from a group and time when your copilot stops answering. Your numbers depend on your configuration.
2. **Know how sensitive content is granted.** In this tenant, direct and SharePoint-group grants took effect within seconds; Entra-group changes took tens of minutes in SharePoint itself.
3. **Make caches and memory permission-aware.** Store the sources of every cached answer and memory item, and re-check the current user's access to all of them on every hit.
4. **Fail closed when permission data is stale.** If your permission state hasn't refreshed recently, deny group-based access rather than trusting old data.
5. **Don't assume "the index has ACLs" covers the agent layer.** It doesn't.

## How the revocation layer works

![Architecture]({{figures.architecture}})

- **Membership:** Microsoft Graph group deltas, polled every few seconds, into its own transitive-membership table.
- **Grants:** SharePoint role assignments, with SharePoint groups expanded into their members.
- **Enforcement:** queries a separate, unfiltered index with a service identity, pages through results, and returns only documents the user may read according to its own state.
- **Fail closed:** if either state source hasn't refreshed within {{meta.stale_after_s|dur}}, group-based access is denied and the service reports degraded.
- **Agent guard:** cache and memory entries carry their source documents; every hit is re-checked for the current user.

It was tested on {{run.documents}} documents. **Its polling approach has not been tested at enterprise scale.**

## Method

- **Synthetic tenant** generated from a spec: users, nested Entra groups, SharePoint groups, folders with unique permissions, documents with canaries. Every run starts from a freshly generated tenant with a new generation id, so canaries from different runs never collide.
- **Oracle:** an independent model of intended access, computed only from the spec and the confirmed changes. It shares no code with the systems under test; a test enforces that.
- **Scoring:** a leak is a canary served while intent denies it; a lockout is a denial while intent allows. Errors and not-yet-indexed results are never counted as denials.
- **Runs:** read every {{run.read_interval_s|dur}}; "stopped" means {{run.n_consecutive}} conforming reads in a row; observation capped at {{run.poll_cap_s|dur}}.
- **Validity rules:** an agent trial only counts if the cache or memory was actually hit with forbidden content; otherwise it is invalid, never "OK".

**Mistakes I found in my own benchmark, and fixed:** stale copies of earlier runs in the search indexes inflated recall costs and could match old canaries — fixed with per-run canaries and index cleanup; a start-up period where my own layer locked users out — fixed with a readiness gate (the cause is still not fully explained); and one agent trial scored "OK" because the source document wasn't indexed yet — re-scored as invalid and re-run. These are documented in the findings log.

## Limitations

- One test tenant, one site, {{run.documents}} documents, {{run.personas}} test users; measured {{meta.measurement_dates}}.
- {{meta.replicates_range}} replicates per cell — descriptive statistics only, no significance claims.
- Azure AI Search measured in its **basic** ACL configuration with the shortest supported indexer schedule; Microsoft's SharePoint-group configuration was not tested.
- The revocation layer queries a separate, unfiltered index over the same data source.
- The tenant admin (a site collection admin) is out of scope.
- Time resolution is {{run.read_interval_s|dur}}.
- In S6, retrieval was not re-run on the turns the cache answered, so the trials say where the leaked content came from, not what retrieval would have returned then.
- S6 uses a stub LLM and deterministic embeddings; other embeddings change how often a naive agent leaks.
- SharePoint's behavior varied between runs in ways I could not explain; the ranges in Figure 2 show that.

## Reproduce it

Everything is public: the harness, oracle, runner, benchmark, revocation layer, redacted run journals, the findings log, and the full HTML report: **{{meta.repo_url}}**.

## About me

I'm Muhammad Hassan Siddiqui, a backend and GenAI engineer (Python, FastAPI, LangGraph, Azure AI Search, Microsoft Graph). I work remotely with teams building Microsoft 365 copilots and RAG systems that need to pass security review. If you'd like to run this harness against your own dev tenant, get in touch: **{{meta.contact}}**.
