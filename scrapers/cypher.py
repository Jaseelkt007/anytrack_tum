"""Cypher templates and write helpers for the GitHub ingestion pipeline.

Append-only invariants (per PHASE_1_PLAN.md):
  - Every edge has first_seen_at (set on CREATE) and last_seen_at (updated on every
    re-observation).
  - Edges are never DELETEd. If a previously-observed edge disappears, set
    removed_at on it (Phase 2 handles this — Phase 1 just doesn't unstar/unfollow).
  - Person canonical_id is deterministic. New Persons encountered through GitHub
    follows get id = uuid5(NAMESPACE, "gh:<handle_lower>"). If the same Person
    is also a known investor with a different identity, they will appear as a
    duplicate in Phase 1 — the resolver in Phase 2 fuses them.
"""

from __future__ import annotations

import uuid

# Same NAMESPACE used by load_investor_reference.py — must not change.
NAMESPACE = uuid.UUID("8e1b3f2a-1c5d-4e7f-9a8b-2c3d4e5f6a7b")


def github_person_id(handle: str) -> str:
    """Deterministic canonical_id for a Person known only by GitHub handle."""
    return str(uuid.uuid5(NAMESPACE, f"gh:{handle.lower()}"))


# --- Person + GitHub identity (used for newly-discovered followed users) -----

UPSERT_PERSON_BY_GITHUB = """
// Normalize handle to lowercase so case variants (jakewharton vs JakeWharton)
// don't create separate PlatformIdentity nodes. Original case is preserved on
// handle_original for display.
WITH toLower($handle) AS handle_lc
MERGE (i:PlatformIdentity {platform: 'github', handle: handle_lc})
ON CREATE SET
    i.handle_original   = $handle,
    i.profile_url       = $profile_url,
    i.verified_via      = 'observed',
    i.confidence        = 0.6,
    i.kind              = $kind,
    i.first_observed_at = datetime($now_iso)
SET i.kind = coalesce(i.kind, $kind)
WITH i
MERGE (p:Person {canonical_id: $canonical_id})
ON CREATE SET
    p.display_name      = $display_name,
    p.role_tags         = ['observed'],
    p.confidence_score  = 0.6,
    p.entity_type       = $kind,
    p.first_observed_at = datetime($now_iso)
SET
    p.last_observed_at = datetime($now_iso),
    p.entity_type      = coalesce(p.entity_type, $kind)
MERGE (p)-[:HAS_IDENTITY]->(i)
RETURN p.canonical_id AS canonical_id
"""

# --- Repository upsert -------------------------------------------------------

UPSERT_REPOSITORY = """
MERGE (r:Repository {github_id: $github_id})
ON CREATE SET
    r.created_at = datetime($now_iso)
SET
    r.owner_handle        = $owner_handle,
    r.name                = $name,
    r.full_name           = $full_name,
    r.description         = $description,
    r.language            = $language,
    r.star_count_observed = $star_count,
    r.html_url            = $html_url,
    r.last_fetched_at     = datetime($now_iso)
"""

# --- Edges -------------------------------------------------------------------

# Watcher (active watchlist member) -> Repository, with the historical starred_at.
MERGE_STARRED_REPO = """
MATCH (p:Person {canonical_id: $watcher_id})
MATCH (r:Repository {github_id: $repo_github_id})
MERGE (p)-[e:STARRED_REPO]->(r)
ON CREATE SET
    e.first_seen_at = datetime($starred_at)
SET e.last_seen_at = datetime($now_iso)
"""

# Watcher -> followed Person. No real timestamp; first_seen_at = poll time.
MERGE_FOLLOWS_GITHUB = """
MATCH (watcher:Person {canonical_id: $watcher_id})
MATCH (followed:Person {canonical_id: $followed_id})
MERGE (watcher)-[e:FOLLOWS_ON_GITHUB]->(followed)
ON CREATE SET
    e.first_seen_at = datetime($now_iso)
SET e.last_seen_at = datetime($now_iso)
"""

# A repo's owner_handle, when matched to a known Person, becomes an OWNS_REPO edge.
# (We only upgrade this opportunistically in Phase 1; not required by M3 acceptance.)
MERGE_OWNS_REPO = """
MATCH (p:Person)-[:HAS_IDENTITY]->(:PlatformIdentity {platform: 'github', handle: $owner_handle})
MATCH (r:Repository {github_id: $repo_github_id})
MERGE (p)-[e:OWNS_REPO]->(r)
ON CREATE SET e.first_seen_at = datetime($now_iso)
"""

# --- Twitter (Phase 2 / M8) --------------------------------------------------

# Person + Twitter identity upsert. Used by the Twitter signal loader after
# resolving canonical_id via identity.resolver.
UPSERT_PERSON_BY_TWITTER = """
WITH toLower($handle) AS handle_lc
MERGE (i:PlatformIdentity {platform: 'twitter', handle: handle_lc})
ON CREATE SET
    i.handle_original   = $handle,
    i.profile_url       = $profile_url,
    i.verified_via      = 'observed',
    i.confidence        = 0.6,
    i.first_observed_at = datetime($now_iso)
WITH i
MERGE (p:Person {canonical_id: $canonical_id})
ON CREATE SET
    p.display_name      = $display_name,
    p.role_tags         = ['observed'],
    p.confidence_score  = 0.6,
    p.first_observed_at = datetime($now_iso)
SET p.last_observed_at = datetime($now_iso),
    p.display_name = coalesce(p.display_name, $display_name)
MERGE (p)-[:HAS_IDENTITY]->(i)
RETURN p.canonical_id AS canonical_id
"""

# Watcher -> followed Person on Twitter. No real follow timestamp; first_seen_at
# is the observation time of the snapshot diff. Append-only: never DELETEd.
MERGE_FOLLOWS_TWITTER = """
MATCH (watcher:Person {canonical_id: $watcher_id})
MATCH (target:Person  {canonical_id: $target_id})
MERGE (watcher)-[e:FOLLOWS_ON_TWITTER]->(target)
ON CREATE SET
    e.first_seen_at = datetime($observed_at),
    e.confidence    = $confidence,
    e.evidence_url  = $evidence_url,
    e.timing_basis  = $timing_basis
SET e.last_seen_at = datetime($observed_at)
"""


# --- Reads -------------------------------------------------------------------

# Active watchlist with their GitHub handles (driver: pipeline orchestrator)
QUERY_ACTIVE_WATCHLIST_WITH_GITHUB = """
MATCH (p:Person)-[w:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
MATCH (p)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform: 'github'})
RETURN p.canonical_id AS canonical_id,
       p.display_name AS display_name,
       i.handle       AS github_handle
ORDER BY p.display_name
"""

# Active + reference angels with their Twitter handles (driver: M8 watchlist build)
QUERY_TWITTER_WATCHERS = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform: 'twitter'})
WHERE
    EXISTS {
        MATCH (p)-[:WATCHED_BY {tier: 'active'}]->(:User {id: $user_id})
    }
    OR p.investor_type = 'Angel'
RETURN DISTINCT p.canonical_id AS canonical_id,
                p.display_name AS display_name,
                i.handle       AS twitter_handle
ORDER BY p.display_name
"""

# Look up a watcher's canonical_id by their twitter handle (driver: signal loader)
QUERY_PERSON_BY_TWITTER_HANDLE = """
MATCH (p:Person)-[:HAS_IDENTITY]->(i:PlatformIdentity {platform: 'twitter', handle: $handle})
RETURN p.canonical_id AS canonical_id LIMIT 1
"""
