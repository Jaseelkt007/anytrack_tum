"""Cypher queries for the FastAPI backend.

All queries assume Phase 1 schema (see scripts/schema.cypher) and the M2/M2.5/M3
data state. They are read-only.
"""

from __future__ import annotations

# --- Health -----------------------------------------------------------------

HEALTH = "RETURN 1 AS ok"


# --- Investors (active watchlist members) ----------------------------------

LIST_INVESTORS = """
MATCH (p:Person)-[w:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(li:PlatformIdentity {platform: 'linkedin'})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(tw:PlatformIdentity {platform: 'twitter'})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
RETURN
  p.canonical_id   AS id,
  p.display_name   AS name,
  p.investor_type  AS investor_type,
  w.archetype      AS archetype,
  p.country        AS country,
  li.profile_url   AS linkedin_url,
  tw.handle        AS twitter_handle,
  gh.handle        AS github_handle
ORDER BY p.display_name
"""


# --- Founder candidates -----------------------------------------------------
# A "founder candidate" in Phase 1 = any Person who is the target of inbound
# FOLLOWS_ON_GITHUB edges from >= MIN_WATCHERS distinct active watchers AND is
# not themselves an active watcher.

LIST_FOUNDER_CANDIDATES = """
// Read from persisted ConvergenceEvent nodes (populated by intelligence.convergence).
// Falls back to live query if no events are persisted yet.
MATCH (c:ConvergenceEvent {user_id: $user_id})-[:ABOUT]->(target:Person)
WHERE c.distinct_member_count >= $min_watchers
OPTIONAL MATCH (target)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
OPTIONAL MATCH (target)-[:HAS_IDENTITY]->(li:PlatformIdentity {platform: 'linkedin'})
OPTIONAL MATCH (target)-[:HAS_IDENTITY]->(tw:PlatformIdentity {platform: 'twitter'})
RETURN
  target.canonical_id        AS id,
  target.display_name        AS name,
  c.distinct_member_count    AS watcher_count,
  c.score                    AS score,
  gh.handle                  AS github_handle,
  gh.profile_url             AS github_url,
  li.profile_url             AS linkedin_url,
  tw.handle                  AS twitter_handle
ORDER BY c.score DESC, watcher_count DESC, target.display_name
LIMIT $limit
"""


# --- Convergence alerts (computed live for Phase 1) -------------------------
# Per founder candidate, return one row per converging signal so the API layer
# can group them into ConvergenceAlert objects.

LIST_CONVERGENCE_SIGNALS = """
// Read from persisted ConvergenceEvent nodes; signals come from evidence_json.
MATCH (c:ConvergenceEvent {user_id: $user_id})-[:ABOUT]->(target:Person)
WHERE c.distinct_member_count >= $min_watchers
OPTIONAL MATCH (target)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
RETURN
  target.canonical_id        AS founder_id,
  target.display_name        AS founder_name,
  gh.handle                  AS github_handle,
  gh.profile_url             AS github_url,
  c.evidence_json            AS evidence_json,
  c.distinct_member_count    AS distinct_watchers,
  c.score                    AS score,
  c.score_breakdown_json     AS score_breakdown_json,
  c.signal_type_counts_json  AS signal_type_counts_json,
  c.first_signal_at          AS first_signal_at,
  c.last_signal_at           AS last_signal_at,
  c.window_start             AS window_start,
  c.window_end               AS window_end,
  c.fired_at                 AS fired_at
ORDER BY c.score DESC, founder_name
LIMIT $limit
"""


# --- Person detail ----------------------------------------------------------

PERSON_DETAIL = """
MATCH (p:Person {canonical_id: $id})
OPTIONAL MATCH (p)-[w:WATCHED_BY]->(:User {id: $user_id})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(li:PlatformIdentity {platform: 'linkedin'})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(tw:PlatformIdentity {platform: 'twitter'})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
RETURN
  p.canonical_id   AS id,
  p.display_name   AS name,
  p.investor_type  AS investor_type,
  p.role_tags      AS role_tags,
  p.country        AS country,
  w.tier           AS watch_tier,
  w.archetype      AS archetype,
  li.profile_url   AS linkedin_url,
  tw.handle        AS twitter_handle,
  gh.handle        AS github_handle,
  gh.profile_url   AS github_url
"""


# --- Recent signals BY a person (their outbound stars + follows) ------------

PERSON_OUTBOUND_SIGNALS = """
MATCH (p:Person {canonical_id: $id})
OPTIONAL MATCH (p)-[s:STARRED_REPO]->(r:Repository)
WITH p, collect({
  type: 'STARRED_REPO',
  repo_full_name: r.full_name,
  repo_html_url: r.html_url,
  first_seen_at: s.first_seen_at
})[0..15] AS stars
OPTIONAL MATCH (p)-[f:FOLLOWS_ON_GITHUB]->(other:Person)
OPTIONAL MATCH (other)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
WITH stars, collect({
  type: 'FOLLOWS_ON_GITHUB',
  followed_name: other.display_name,
  followed_handle: gh.handle,
  followed_url: gh.profile_url,
  first_seen_at: f.first_seen_at
})[0..15] AS follows
RETURN stars, follows
"""


# --- Inbound signals TO a person (who follows / stars their repos) ----------

PERSON_INBOUND_SIGNALS = """
MATCH (target:Person {canonical_id: $id})
OPTIONAL MATCH (w:Person)-[f:FOLLOWS_ON_GITHUB]->(target)
WHERE (w)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
OPTIONAL MATCH (w)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
WITH target, collect(DISTINCT {
  watcher_id: w.canonical_id,
  watcher_name: w.display_name,
  watcher_github: gh.handle,
  type: 'FOLLOWS_ON_GITHUB',
  first_seen_at: f.first_seen_at
}) AS inbound_follows
RETURN inbound_follows
"""


# --- Graph snapshot for the Explore page -----------------------------------
# Returns nodes (active investors + founder candidates) and edges between them.
# Capped to keep React Flow snappy.

GRAPH_NODES = """
MATCH (p:Person)-[w:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(tw:PlatformIdentity {platform: 'twitter'})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(li:PlatformIdentity {platform: 'linkedin'})
RETURN
  p.canonical_id  AS id,
  p.display_name  AS name,
  p.investor_type AS investor_type,
  w.archetype     AS archetype,
  p.country       AS country,
  gh.handle       AS github_handle,
  tw.handle       AS twitter_handle,
  li.profile_url  AS linkedin_url,
  'investor'      AS kind
"""

GRAPH_EDGES = """
MATCH (w:Person)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
MATCH (w)-[edge:FOLLOWS_ON_GITHUB]->(target:Person)
WHERE NOT (target)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
WITH target, w, edge
WITH target,
     collect({watcher: w, edge_first_seen: edge.first_seen_at}) AS watchers
WHERE size(watchers) >= $min_watchers
UNWIND watchers AS w_entry
OPTIONAL MATCH (target)-[:HAS_IDENTITY]->(gh:PlatformIdentity {platform: 'github'})
RETURN
  target.canonical_id AS founder_id,
  target.display_name AS founder_name,
  gh.handle           AS founder_github,
  w_entry.watcher.canonical_id AS watcher_id,
  w_entry.watcher.display_name AS watcher_name,
  w_entry.edge_first_seen      AS first_seen_at
ORDER BY founder_name, watcher_name
LIMIT $edge_limit
"""
