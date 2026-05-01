"""Enrichment fetcher (M9.5.1) — gathers per-target evidence into a single
JSON-serializable bundle that the classifier (M9.5.2) consumes.

Pure orchestration over Neo4j + optional GitHub / Scrapebadger clients. No
LLM calls here. No persistence. Caller decides whether to feed the bundle
to the classifier and whether to cache.

Data sources (each independent; missing data -> field is None / empty list):

  - target_person          : Person node (graph)
  - github_profile         : live GitHub /users/{handle} call (if client given)
  - owned_repos            : OWNS_REPO edges in graph (top 5 by stars)
  - twitter_profile        : Scrapebadger lookup_user (if client given)
  - recent_tweets          : Scrapebadger latest_tweets (if client given)
  - convergence_evidence   : latest ConvergenceEvent for (user, target)
  - cross_platform_followers : every watcher who follows target on any platform
  - kb_match               : whether target is in the reference investor set
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --- Component dataclasses -------------------------------------------------

@dataclass(frozen=True)
class GitHubProfile:
    handle: str
    profile_url: str
    name: str = ""
    bio: str = ""
    location: str = ""
    company: str = ""
    blog: str = ""
    public_repos: int = 0
    followers: int = 0
    following: int = 0


@dataclass(frozen=True)
class OwnedRepo:
    full_name: str
    html_url: str
    stars: int = 0
    language: str = ""
    description: str = ""


@dataclass(frozen=True)
class TwitterProfile:
    handle: str
    profile_url: str
    display_name: str = ""
    verified: bool = False
    followers_count: int = 0
    following_count: int = 0


@dataclass(frozen=True)
class TweetSummary:
    id: str
    text: str
    created_at: str
    url: str
    favorite_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    view_count: int = 0


@dataclass(frozen=True)
class ConvergenceEvidence:
    event_id: str
    distinct_member_count: int
    score: float
    window_start: str
    window_end: str
    signal_type_counts: dict[str, int]
    evidence_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class CrossPlatformFollower:
    platform: str          # 'github' | 'twitter'
    canonical_id: str
    display_name: str


@dataclass(frozen=True)
class KBMatch:
    is_known: bool
    investor_type: str | None = None
    country: str | None = None
    sector_tags: list[str] = field(default_factory=list)
    role_tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TargetPersonRef:
    canonical_id: str
    display_name: str
    role_tags: list[str] = field(default_factory=list)
    identities: list[dict[str, str]] = field(default_factory=list)  # [{platform, handle, profile_url}]


@dataclass(frozen=True)
class EnrichmentBundle:
    target_person: TargetPersonRef
    github_profile: GitHubProfile | None
    owned_repos: list[OwnedRepo]
    twitter_profile: TwitterProfile | None
    recent_tweets: list[TweetSummary]      # empty list if not fetched
    convergence_evidence: ConvergenceEvidence | None
    cross_platform_followers: list[CrossPlatformFollower]
    kb_match: KBMatch
    user_id: str
    gathered_at: str

    def to_json(self) -> str:
        """Stable JSON string. Used by M9.5.2 to compute the bundle hash for
        idempotency. `gathered_at` is excluded because it changes every run."""
        d = asdict(self)
        d.pop("gathered_at", None)
        return json.dumps(d, sort_keys=True, default=str)


# --- Cypher templates ------------------------------------------------------

_QUERY_TARGET_PERSON = """
MATCH (p:Person {canonical_id: $target_id})
OPTIONAL MATCH (p)-[:HAS_IDENTITY]->(i:PlatformIdentity)
RETURN p.canonical_id  AS canonical_id,
       p.display_name  AS display_name,
       coalesce(p.role_tags, []) AS role_tags,
       collect(CASE WHEN i IS NULL THEN NULL
                    ELSE {platform: i.platform, handle: i.handle, profile_url: i.profile_url}
               END) AS identities
"""

_QUERY_OWNED_REPOS = """
MATCH (p:Person {canonical_id: $target_id})-[:OWNS_REPO]->(r:Repository)
RETURN r.full_name           AS full_name,
       r.html_url             AS html_url,
       coalesce(r.star_count_observed, 0) AS stars,
       coalesce(r.language, '') AS language,
       coalesce(r.description, '') AS description
ORDER BY stars DESC, full_name
LIMIT 5
"""

_QUERY_LATEST_CONVERGENCE = """
MATCH (c:ConvergenceEvent {user_id: $user_id})-[:ABOUT]->(p:Person {canonical_id: $target_id})
RETURN c.id AS event_id,
       c.distinct_member_count AS n,
       c.score AS score,
       toString(c.window_start) AS window_start,
       toString(c.window_end)   AS window_end,
       c.signal_type_counts_json AS signal_type_counts_json,
       c.evidence_json AS evidence_json
ORDER BY c.window_end DESC
LIMIT 1
"""

_QUERY_CROSS_PLATFORM_FOLLOWERS = """
MATCH (w:Person)-[:WATCHED_BY]->(:User {id: $user_id})
MATCH (w)-[:FOLLOWS_ON_GITHUB]->(p:Person {canonical_id: $target_id})
RETURN 'github' AS platform, w.canonical_id AS id, w.display_name AS name

UNION

MATCH (w:Person)-[:WATCHED_BY]->(:User {id: $user_id})
MATCH (w)-[:FOLLOWS_ON_TWITTER]->(p:Person {canonical_id: $target_id})
RETURN 'twitter' AS platform, w.canonical_id AS id, w.display_name AS name
"""

_QUERY_KB_MATCH = """
MATCH (p:Person {canonical_id: $target_id})
RETURN coalesce(p.investor_type, '') AS investor_type,
       coalesce(p.country, '')        AS country,
       coalesce(p.sector_tags, [])    AS sector_tags,
       coalesce(p.role_tags, [])      AS role_tags
"""


# --- Internal helpers ------------------------------------------------------

def _fetch_target_person(session, target_id: str) -> TargetPersonRef | None:
    rec = session.run(_QUERY_TARGET_PERSON, target_id=target_id).single()
    if not rec:
        return None
    raw_ids = rec["identities"] or []
    identities = [dict(x) for x in raw_ids if x]
    return TargetPersonRef(
        canonical_id=rec["canonical_id"],
        display_name=rec["display_name"] or "",
        role_tags=list(rec["role_tags"] or []),
        identities=identities,
    )


def _fetch_owned_repos(session, target_id: str) -> list[OwnedRepo]:
    rows = session.run(_QUERY_OWNED_REPOS, target_id=target_id).data()
    return [OwnedRepo(
        full_name=r["full_name"] or "",
        html_url=r["html_url"] or "",
        stars=int(r["stars"] or 0),
        language=r["language"] or "",
        description=r["description"] or "",
    ) for r in rows]


def _fetch_convergence_evidence(session, user_id: str, target_id: str) -> ConvergenceEvidence | None:
    rec = session.run(_QUERY_LATEST_CONVERGENCE,
                      user_id=user_id, target_id=target_id).single()
    if not rec:
        return None
    try:
        signal_counts = json.loads(rec["signal_type_counts_json"] or "{}")
    except json.JSONDecodeError:
        signal_counts = {}
    try:
        evidence_rows = json.loads(rec["evidence_json"] or "[]")
    except json.JSONDecodeError:
        evidence_rows = []
    return ConvergenceEvidence(
        event_id=rec["event_id"],
        distinct_member_count=int(rec["n"] or 0),
        score=float(rec["score"] or 0.0),
        window_start=rec["window_start"] or "",
        window_end=rec["window_end"] or "",
        signal_type_counts=signal_counts,
        evidence_rows=evidence_rows,
    )


def _fetch_cross_platform_followers(session, user_id: str, target_id: str) -> list[CrossPlatformFollower]:
    rows = session.run(_QUERY_CROSS_PLATFORM_FOLLOWERS,
                       user_id=user_id, target_id=target_id).data()
    return [CrossPlatformFollower(
        platform=r["platform"],
        canonical_id=r["id"],
        display_name=r["name"] or r["id"],
    ) for r in rows]


def _fetch_kb_match(session, target_id: str) -> KBMatch:
    rec = session.run(_QUERY_KB_MATCH, target_id=target_id).single()
    if not rec:
        return KBMatch(is_known=False)
    investor_type = (rec["investor_type"] or "").strip()
    return KBMatch(
        is_known=bool(investor_type),
        investor_type=investor_type or None,
        country=(rec["country"] or "").strip() or None,
        sector_tags=list(rec["sector_tags"] or []),
        role_tags=list(rec["role_tags"] or []),
    )


def _identity_handle(target: TargetPersonRef, platform: str) -> tuple[str, str] | None:
    """Return (handle, profile_url) for the target on `platform`, or None."""
    for ident in target.identities:
        if ident.get("platform") == platform:
            return ident.get("handle", ""), ident.get("profile_url", "")
    return None


def _fetch_github_profile(target: TargetPersonRef, github_client) -> GitHubProfile | None:
    pair = _identity_handle(target, "github")
    if not pair:
        return None
    handle, profile_url = pair
    if not handle:
        return None
    if github_client is None:
        # Best-effort minimal record from graph data.
        return GitHubProfile(handle=handle, profile_url=profile_url or f"https://github.com/{handle}")
    try:
        body = github_client.get(f"users/{handle}")
    except Exception as e:
        logger.warning("github lookup failed for %s: %s", handle, e)
        body = None
    if not body or not isinstance(body, dict):
        return GitHubProfile(handle=handle, profile_url=profile_url or f"https://github.com/{handle}")
    return GitHubProfile(
        handle=handle,
        profile_url=body.get("html_url") or profile_url or f"https://github.com/{handle}",
        name=body.get("name") or "",
        bio=body.get("bio") or "",
        location=body.get("location") or "",
        company=body.get("company") or "",
        blog=body.get("blog") or "",
        public_repos=int(body.get("public_repos") or 0),
        followers=int(body.get("followers") or 0),
        following=int(body.get("following") or 0),
    )


def _fetch_twitter_profile(target: TargetPersonRef, twitter_client) -> TwitterProfile | None:
    pair = _identity_handle(target, "twitter")
    if not pair:
        return None
    handle, profile_url = pair
    if not handle:
        return None
    base = TwitterProfile(
        handle=handle,
        profile_url=profile_url or f"https://x.com/{handle}",
    )
    if twitter_client is None:
        return base
    try:
        rec = twitter_client.lookup_user(handle)
    except Exception as e:
        logger.warning("twitter lookup failed for %s: %s", handle, e)
        return base
    return TwitterProfile(
        handle=handle,
        profile_url=base.profile_url,
        display_name=rec.name,
        verified=rec.verified,
        followers_count=rec.followers_count,
        following_count=rec.following_count,
    )


def _fetch_recent_tweets(twitter_handle: str | None, twitter_client, *, limit: int = 10) -> list[TweetSummary]:
    if not twitter_handle or twitter_client is None:
        return []
    try:
        page = twitter_client.list_latest_tweets(twitter_handle)
    except Exception as e:
        logger.warning("twitter latest_tweets failed for %s: %s", twitter_handle, e)
        return []
    out: list[TweetSummary] = []
    for t in page.tweets[:limit]:
        out.append(TweetSummary(
            id=t.id,
            text=t.text,
            created_at=t.created_at,
            url=f"https://x.com/{twitter_handle}/status/{t.id}" if t.id else "",
            favorite_count=t.favorite_count,
            retweet_count=t.retweet_count,
            reply_count=t.reply_count,
            view_count=t.view_count,
        ))
    return out


# --- Public API ------------------------------------------------------------

class TargetNotFoundError(LookupError):
    """Raised when the target canonical_id has no Person node in the graph."""


def enrich(
    session,
    target_canonical_id: str,
    *,
    user_id: str = "demo",
    github_client=None,
    twitter_client=None,
    tweet_limit: int = 10,
) -> EnrichmentBundle:
    """Build a full enrichment bundle for one target.

    The returned bundle is JSON-serializable (`bundle.to_json()`). All clients
    are optional: missing a client just means the corresponding section will be
    minimal (graph-only data, no live fetch).
    """
    target = _fetch_target_person(session, target_canonical_id)
    if target is None:
        raise TargetNotFoundError(f"no Person with canonical_id={target_canonical_id}")

    github_profile = _fetch_github_profile(target, github_client)
    owned_repos = _fetch_owned_repos(session, target_canonical_id)
    twitter_profile = _fetch_twitter_profile(target, twitter_client)
    recent_tweets = _fetch_recent_tweets(
        twitter_profile.handle if twitter_profile else None,
        twitter_client,
        limit=tweet_limit,
    )
    convergence_evidence = _fetch_convergence_evidence(session, user_id, target_canonical_id)
    cross_platform_followers = _fetch_cross_platform_followers(session, user_id, target_canonical_id)
    kb_match = _fetch_kb_match(session, target_canonical_id)

    return EnrichmentBundle(
        target_person=target,
        github_profile=github_profile,
        owned_repos=owned_repos,
        twitter_profile=twitter_profile,
        recent_tweets=recent_tweets,
        convergence_evidence=convergence_evidence,
        cross_platform_followers=cross_platform_followers,
        kb_match=kb_match,
        user_id=user_id,
        gathered_at=datetime.now(timezone.utc).isoformat(),
    )
