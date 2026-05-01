"""Unit tests for the M9.5.1 enrichment fetcher.

No Neo4j, no GitHub, no Scrapebadger. Uses a FakeSession that maps Cypher
query substrings to canned results, plus simple fake clients.

Run:
    python intelligence/dossier/test_enrichment.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from intelligence.dossier.enrichment import (
    EnrichmentBundle,
    TargetNotFoundError,
    enrich,
)
from scrapers.clients.twitter_following_client import (
    LatestTweetsPage,
    TweetRecord,
    TwitterUserRecord,
)


# --- Fakes ---------------------------------------------------------------

class FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = list(rows)

    def single(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def data(self) -> list[dict]:
        return list(self._rows)


class FakeSession:
    """Maps Cypher-query *substrings* to canned row lists. The key is matched
    by `if substring in actual_query`, so any unique snippet works."""

    def __init__(self, mapping: dict[str, list[dict]] | None = None):
        self.mapping = mapping or {}
        self.calls: list[tuple[str, dict]] = []

    def run(self, query: str, **params) -> FakeResult:
        self.calls.append((query, params))
        for substr, rows in self.mapping.items():
            if substr in query:
                return FakeResult(rows)
        return FakeResult([])


class FakeGitHubClient:
    def __init__(self, profiles: dict[str, dict] | None = None):
        self.profiles = profiles or {}
        self.calls: list[str] = []

    def get(self, path: str, **kw):
        self.calls.append(path)
        # path looks like 'users/AntonOsika'
        if path.startswith("users/"):
            handle = path[len("users/"):]
            return self.profiles.get(handle.lower())
        return None


class FakeTwitterClient:
    def __init__(
        self,
        users: dict[str, TwitterUserRecord] | None = None,
        tweets: dict[str, list[TweetRecord]] | None = None,
    ):
        self.users = users or {}
        self.tweets = tweets or {}
        self.lookup_calls: list[str] = []
        self.tweets_calls: list[str] = []

    def lookup_user(self, username: str) -> TwitterUserRecord:
        self.lookup_calls.append(username)
        u = self.users.get(username.lower())
        if u is None:
            raise RuntimeError(f"user not found: {username}")
        return u

    def list_latest_tweets(self, username: str, cursor: str | None = None) -> LatestTweetsPage:
        self.tweets_calls.append(username)
        ts = self.tweets.get(username.lower(), [])
        return LatestTweetsPage(tweets=list(ts), next_cursor=None)


# --- Fixture builders ----------------------------------------------------

def _anton_session(*, with_convergence=True, with_owned=True, with_kb=False) -> FakeSession:
    target_id = "anton-id"
    rows = {
        "MATCH (p:Person {canonical_id: $target_id})\nOPTIONAL MATCH (p)-[:HAS_IDENTITY]": [{
            "canonical_id": target_id,
            "display_name": "Anton Osika",
            "role_tags": ["founder_candidate"],
            "identities": [
                {"platform": "github", "handle": "AntonOsika",
                 "profile_url": "https://github.com/AntonOsika"},
                {"platform": "twitter", "handle": "antonosika",
                 "profile_url": "https://twitter.com/antonosika"},
            ],
        }],
    }
    if with_owned:
        rows["MATCH (p:Person {canonical_id: $target_id})-[:OWNS_REPO]"] = [
            {"full_name": "AntonOsika/gpt-engineer",
             "html_url": "https://github.com/AntonOsika/gpt-engineer",
             "stars": 30421, "language": "Python",
             "description": "Specify what you want it to build"},
        ]
    if with_convergence:
        rows["MATCH (c:ConvergenceEvent {user_id: $user_id})-[:ABOUT]"] = [{
            "event_id": "cv-demo-anton-id-2024-11-01",
            "n": 4, "score": 5.0,
            "window_start": "2024-01-01T00:00:00+00:00",
            "window_end": "2024-11-01T00:00:00+00:00",
            "signal_type_counts_json": json.dumps({"STARRED_REPO": 4}),
            "evidence_json": json.dumps([
                {"watcher_name": "Zack Jackson", "evidence_url": "https://github.com/AntonOsika/gpt-engineer/stargazers"},
                {"watcher_name": "Elie Habib", "evidence_url": "https://github.com/AntonOsika/gpt-engineer/stargazers"},
            ]),
        }]
    rows["RETURN 'github' AS platform"] = [
        {"platform": "github", "id": "w1", "name": "John Resig"},
        {"platform": "github", "id": "w2", "name": "Zack Jackson"},
    ]
    if with_kb:
        rows["RETURN coalesce(p.investor_type"] = [
            {"investor_type": "Angel", "country": "USA",
             "sector_tags": ["AI/ML"], "role_tags": ["investor", "angel"]},
        ]
    else:
        rows["RETURN coalesce(p.investor_type"] = [
            {"investor_type": "", "country": "",
             "sector_tags": [], "role_tags": ["founder_candidate"]},
        ]
    return FakeSession(rows)


# --- Tests ---------------------------------------------------------------

def test_enrich_full_bundle_for_anton_with_clients():
    session = _anton_session()
    gh = FakeGitHubClient(profiles={
        "antonosika": {
            "html_url": "https://github.com/AntonOsika",
            "name": "Anton Osika",
            "bio": "Building Lovable. Previously GPT Engineer, Sana.",
            "location": "Stockholm",
            "company": "@lovable-dev",
            "blog": "https://lovable.dev",
            "public_repos": 42,
            "followers": 5000,
            "following": 200,
        },
    })
    tw = FakeTwitterClient(
        users={"antonosika": TwitterUserRecord(
            id="123", username="antonosika", name="Anton Osika",
            verified=False, followers_count=8000, following_count=300,
        )},
        tweets={"antonosika": [
            TweetRecord(id="t1", text="Building agents", created_at="2026-04-30T12:00:00Z",
                        favorite_count=120, retweet_count=15, view_count=10000),
        ]},
    )
    b = enrich(session, "anton-id", user_id="demo",
               github_client=gh, twitter_client=tw)

    assert isinstance(b, EnrichmentBundle)
    assert b.target_person.display_name == "Anton Osika"
    assert b.github_profile is not None
    assert b.github_profile.handle == "AntonOsika"
    assert b.github_profile.location == "Stockholm"
    assert b.github_profile.followers == 5000
    assert b.twitter_profile is not None
    assert b.twitter_profile.followers_count == 8000
    assert len(b.owned_repos) == 1
    assert b.owned_repos[0].full_name == "AntonOsika/gpt-engineer"
    assert b.owned_repos[0].stars == 30421
    assert len(b.recent_tweets) == 1
    assert b.recent_tweets[0].url == "https://x.com/antonosika/status/t1"
    assert b.convergence_evidence is not None
    assert b.convergence_evidence.distinct_member_count == 4
    assert {f.platform for f in b.cross_platform_followers} == {"github"}
    assert b.kb_match.is_known is False  # founder, not investor
    print("  OK  full Anton bundle: github + twitter + repos + tweets + convergence + cross-followers")


def test_enrich_target_missing_raises():
    session = FakeSession({})  # no rows for anything
    try:
        enrich(session, "ghost-id", user_id="demo")
        raise AssertionError("expected TargetNotFoundError")
    except TargetNotFoundError:
        print("  OK  enrich raises TargetNotFoundError when target Person doesn't exist")


def test_enrich_no_clients_returns_minimal_profiles():
    """Without GitHub/Twitter clients, github_profile and twitter_profile carry
    only handle + profile_url derived from the graph."""
    session = _anton_session()
    b = enrich(session, "anton-id", user_id="demo")
    assert b.github_profile is not None and b.github_profile.handle == "AntonOsika"
    assert b.github_profile.bio == ""  # no live fetch
    assert b.twitter_profile is not None and b.twitter_profile.handle == "antonosika"
    assert b.twitter_profile.followers_count == 0
    assert b.recent_tweets == []
    print("  OK  no clients -> minimal github + twitter profile, no tweets, no errors")


def test_enrich_kb_match_when_target_is_known_investor():
    session = _anton_session(with_kb=True)
    b = enrich(session, "anton-id", user_id="demo")
    assert b.kb_match.is_known is True
    assert b.kb_match.investor_type == "Angel"
    assert b.kb_match.country == "USA"
    assert "AI/ML" in b.kb_match.sector_tags
    print("  OK  kb_match populated when target is a known investor")


def test_enrich_kb_miss_for_founder_candidate():
    session = _anton_session(with_kb=False)
    b = enrich(session, "anton-id", user_id="demo")
    assert b.kb_match.is_known is False
    assert b.kb_match.investor_type is None
    print("  OK  kb_match.is_known False when target is not in the reference investor set")


def test_enrich_handles_missing_convergence_event_gracefully():
    session = _anton_session(with_convergence=False)
    b = enrich(session, "anton-id", user_id="demo")
    assert b.convergence_evidence is None
    # rest of the bundle still populated
    assert b.target_person.display_name == "Anton Osika"
    print("  OK  missing ConvergenceEvent -> field is None, rest of bundle intact")


def test_enrich_bundle_is_json_serializable():
    """to_json() produces valid JSON and excludes the moving gathered_at
    field (idempotent across runs for the same data)."""
    session = _anton_session()
    b = enrich(session, "anton-id", user_id="demo")
    raw = b.to_json()
    parsed = json.loads(raw)
    assert "gathered_at" not in parsed, "gathered_at should be excluded for hash stability"
    assert parsed["target_person"]["display_name"] == "Anton Osika"
    # Stable: re-serializing same bundle yields same string
    assert b.to_json() == b.to_json()
    print("  OK  bundle.to_json() is valid JSON, stable, excludes gathered_at")


def test_enrich_github_only_target_has_no_twitter_profile():
    """A target with only a github identity gets twitter_profile=None."""
    target_id = "gh-only"
    session = FakeSession({
        "MATCH (p:Person {canonical_id: $target_id})\nOPTIONAL MATCH (p)-[:HAS_IDENTITY]": [{
            "canonical_id": target_id,
            "display_name": "GH Only",
            "role_tags": ["observed"],
            "identities": [{"platform": "github", "handle": "ghonly",
                           "profile_url": "https://github.com/ghonly"}],
        }],
        "RETURN coalesce(p.investor_type": [{
            "investor_type": "", "country": "", "sector_tags": [], "role_tags": []
        }],
    })
    b = enrich(session, target_id, user_id="demo")
    assert b.twitter_profile is None
    assert b.github_profile is not None
    assert b.recent_tweets == []
    print("  OK  github-only target: twitter_profile is None, no tweet fetch attempted")


def test_enrich_tolerates_twitter_lookup_failure():
    """If Scrapebadger errors during lookup, twitter_profile falls back to
    handle+url only and does NOT raise."""
    session = _anton_session()
    class BoomTwitter(FakeTwitterClient):
        def lookup_user(self, username):
            raise RuntimeError("simulated 500")
        def list_latest_tweets(self, username, cursor=None):
            raise RuntimeError("simulated 500")
    b = enrich(session, "anton-id", user_id="demo", twitter_client=BoomTwitter())
    assert b.twitter_profile is not None
    assert b.twitter_profile.handle == "antonosika"
    assert b.twitter_profile.followers_count == 0  # fallback
    assert b.recent_tweets == []
    print("  OK  twitter client error -> graceful fallback, no exception")


# --- Test runner ----------------------------------------------------------

TESTS = [
    test_enrich_full_bundle_for_anton_with_clients,
    test_enrich_target_missing_raises,
    test_enrich_no_clients_returns_minimal_profiles,
    test_enrich_kb_match_when_target_is_known_investor,
    test_enrich_kb_miss_for_founder_candidate,
    test_enrich_handles_missing_convergence_event_gracefully,
    test_enrich_bundle_is_json_serializable,
    test_enrich_github_only_target_has_no_twitter_profile,
    test_enrich_tolerates_twitter_lookup_failure,
]


def main() -> int:
    print(f"Running {len(TESTS)} M9.5.1 enrichment tests...\n")
    failures = 0
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
