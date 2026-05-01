# API key inventory — Phase 1

Single source of truth for which keys we need, who owns provisioning, and what scopes they require. **Never paste real key values in this file.** Real values live in each developer's local `.env`.

## Required for M1 acceptance

| Key | Owner | Scope / plan | Where to provision | Status |
|---|---|---|---|---|
| `NEO4J_URI` + `NEO4J_USER` + `NEO4J_PASSWORD` | P1 (provisions shared instance) | AuraDB Free (200K nodes / 400K rels) | <https://console.neo4j.io> | ☐ provisioned ☐ shared with team |
| `GITHUB_TOKEN` ×4 | each team member individually | Fine-grained PAT, **public read** only (no scopes needed for public data) | <https://github.com/settings/tokens> | ☐ P1 ☐ P2 ☐ P3 ☐ P4 |
| `GITHUB_USERNAME` | each team member individually | n/a | n/a | ☐ P1 ☐ P2 ☐ P3 ☐ P4 |

The four GitHub PATs together give us 4 × 5,000 = 20,000 requests/hour. The M3 ingestion pipeline budget assumes this.

## Optional in Phase 1 (required later)

| Key | Required for | Plan | Where to provision |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Phase 2 — "why now" NLG and Phase 2 identity-resolver arbitration | Pay-as-you-go, expect <$5 for the demo | <https://console.anthropic.com> |
| Vercel account | M7 frontend deploy | Hobby (free) | <https://vercel.com> |
| Resend API key | Phase 4 email notifications | Free tier 100/day | <https://resend.com> |

## Key handling rules

- **Never commit `.env`.** `.gitignore` blocks it; double-check with `git status` before any commit.
- **Never paste a key in a chat message** that gets logged or persisted (Slack, GitHub issue, PR). Share via a password manager or ephemeral channel.
- **Rotate immediately if leaked.** GitHub PATs can be revoked at <https://github.com/settings/tokens>; Anthropic keys at the console.
- **Per-team-member PATs, not a shared one.** If one PAT is revoked or rate-limited, the others still work.

## Scope notes — GitHub PAT

For Phase 1 we only read public data (stars, public follows, public profiles). The fine-grained PAT can have **no permissions selected** and still work. Choose:

- **Resource owner**: your personal account
- **Repository access**: "Public repositories (read-only)" *(this is automatic with no scopes)*
- **Permissions**: leave all defaults

This minimizes blast radius if a token leaks.

## Verification

Run `python scripts/healthcheck.py` — it confirms each laptop's keys without printing them.
