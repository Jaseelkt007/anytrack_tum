// Phase 1 schema constraints and indexes.
// Apply with: python scripts/apply_schema.py
// Verify with: SHOW CONSTRAINTS in Neo4j Browser.
//
// STABLE per PHASE_1_PLAN.md — do not modify without team sign-off.

// --- Uniqueness constraints (also create implicit indexes) ---

CREATE CONSTRAINT person_canonical_id IF NOT EXISTS
FOR (p:Person) REQUIRE p.canonical_id IS UNIQUE;

CREATE CONSTRAINT repository_github_id IF NOT EXISTS
FOR (r:Repository) REQUIRE r.github_id IS UNIQUE;

CREATE CONSTRAINT user_id IF NOT EXISTS
FOR (u:User) REQUIRE u.id IS UNIQUE;

CREATE CONSTRAINT convergence_event_id IF NOT EXISTS
FOR (c:ConvergenceEvent) REQUIRE c.id IS UNIQUE;

// PlatformIdentity is unique by (platform, handle) composite.
CREATE CONSTRAINT platform_identity_handle IF NOT EXISTS
FOR (i:PlatformIdentity) REQUIRE (i.platform, i.handle) IS UNIQUE;

// --- Lookup indexes for common queries ---

CREATE INDEX person_display_name IF NOT EXISTS
FOR (p:Person) ON (p.display_name);

CREATE INDEX person_investor_type IF NOT EXISTS
FOR (p:Person) ON (p.investor_type);

CREATE INDEX person_country IF NOT EXISTS
FOR (p:Person) ON (p.country);

// --- Edge property indexes for temporal queries ---
// Required for the convergence query's WHERE r.first_seen_at > $cutoff predicate.

CREATE INDEX starred_repo_first_seen IF NOT EXISTS
FOR ()-[r:STARRED_REPO]-() ON (r.first_seen_at);

CREATE INDEX follows_github_first_seen IF NOT EXISTS
FOR ()-[r:FOLLOWS_ON_GITHUB]-() ON (r.first_seen_at);

CREATE INDEX watched_by_added_at IF NOT EXISTS
FOR ()-[r:WATCHED_BY]-() ON (r.added_at);
