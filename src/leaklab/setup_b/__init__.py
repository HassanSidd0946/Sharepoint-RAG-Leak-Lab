"""Setup B: our own layer in front of the same Azure AI Search index (revocation overlay).

state/   the authoritative state: who is in which group, and who holds which role on which scope,
         kept current by pollers (Graph group delta, Graph drive delta + SharePoint REST) in
         Postgres. No API yet. The oracle is never imported here
         (tests/test_setup_b_isolation.py enforces it).
"""
