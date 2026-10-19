# 2-minute video: screen recording + voice-over

**Format:** 1920×1080, presentation mode, dark theme. Voice-over recorded separately and laid
over the screen capture. No face.

**Bundle to record:** after the overnight runs, the **S1-entra replicate closest to the median
across all three systems** (the exporter's ranking). Not the most dramatic one. Say so on
screen: "Replicate closest to the median."

**Playback:** scripted (see Prompt 24, part D), so speed changes are identical every take and
the "Real run, played back at Nx" label always shows the true speed:

| Replay time (t_rel) | Speed | Video duration | What happens on screen |
|---|---|---|---|
| −3 s → +12 s | 1× | ~15 s | Change confirmed at 0; revocation-layer lane turns red at ~10 s |
| +12 s → +6 min | 60× | ~6 s | Azure AI Search lane turns red (minutes) |
| +6 min → end of bundle window | 300× | ~11 s | SharePoint keeps serving; turns red at ~40+ min; flapping visible |

Spoken numbers below are tokens: fill them from the final report with the same fill step
as the write-up. Never read numbers from the provisional n=1 handoff.

---

## Script

| Time | Screen | Voice-over |
|---|---|---|
| **0:00–0:08** | Black. Text fades in: *"You remove someone from a security group. How long can your copilot still show them the file?"* | *(silence, or a single line)* "You remove someone from a security group. How long can your copilot still show them the file?" |
| **0:08–0:22** | Replay page, paused at t = −3 s. Highlight the three lane labels one by one, then the config caption under the Azure lane. | "I measured it. Same Microsoft 365 test tenant, same change, three systems: SharePoint itself, Azure AI Search with its SharePoint permission indexing in the basic configuration, and a small revocation layer I built." |
| **0:22–0:37** | Play at 1×. Red "change confirmed" line appears; ~10 s later the revocation-layer clock freezes red. | "Paul is removed from the group. Entra confirms it. Ten seconds later, the revocation layer stops serving his file. The other two are still serving it." |
| **0:37–0:43** | Speed switches to 60× (label updates). Azure lane turns red. | "Azure AI Search stops after about {{S1-entra.setup_a_basic.first_deny_s.this_bundle\|dur}}." |
| **0:43–0:55** | Speed 300×. SharePoint lane stays green for a long time, then red, then flickers. | "SharePoint itself kept serving the file for {{S1-entra.setup0.first_deny_s.this_bundle\|dur}} — and then flipped back and forth for a while." |
| **0:55–1:08** | Cut to Figure 2 (dot plot), every replicate visible. | "Across {{S1-entra.setup0.n}} runs, the median was {{S1-entra.setup0.first_deny_s.median\|dur}}. To be fair: for direct grants, SharePoint revokes in seconds. The gap is group-based access." |
| **1:08–1:22** | S6 view: finance lead asks a question; cut to the intern asking a paraphrase; the naive agent's answer appears with the finance lead's reference code highlighted red. | "Second finding. Retrieval was permission-trimmed in every case. But an agent with a shared answer cache handed the finance lead's answer to an intern — in every run." |
| **1:22–1:35** | Same question to the guarded agent: denied. Then the positive control: served, green. | "The guarded agent checks every source of a cached answer, for the current user, every time. It blocked every attempt — and still served people who were allowed." |
| **1:35–1:50** | Architecture diagram. | "How it works: Graph deltas, its own permission state, a check on every result and every cache hit, and it fails closed when that state is stale." |
| **1:50–2:00** | Name, repo URL, one line of text. | "One test tenant, so test your own. The harness is open source — link below. I help teams ship Microsoft 365 copilots that pass security review." |

---

## Recording notes
- Record each segment separately; join in any basic editor (cuts only, no effects).
- The speed label and the "Replicate closest to the median" caption must be visible in every replay frame.
- Read the spoken numbers from the filled script, not from memory.
- Voice: your own voice. If you use a voice clone, say so in the post ("narration uses an AI clone of my voice").
- Keep a 1-line on-screen caveat during 0:22–0:55: "One M365 test tenant · Azure AI Search basic ACL configuration".
