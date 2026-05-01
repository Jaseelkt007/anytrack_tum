"""Email digest delivery via Resend (M12-email).

Daily job that drains the dossier outbox: queries `ready_to_send` dossiers
matching the user's quality thresholds, formats them as an HTML digest,
sends via Resend, and on success flips each dossier's status to `sent`
(per the M9.5 immutability contract — sent dossiers are never re-emailed,
re-classifications create new dossiers alongside).

Idempotent at the dossier level via the status flip — re-running on the
same data is a no-op.

Configuration lives on AlertRule (per-user) — `notify_email`, `notify_enabled`,
`notify_daily_cap`, `notify_min_score`, `notify_min_confidence`,
`notify_classifications`. Sender domain + API key live in env (RESEND_API_KEY,
NOTIFY_FROM_EMAIL, NOTIFY_FROM_NAME).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from intelligence.rule import AlertRule, get_rule

logger = logging.getLogger(__name__)


# --- Cypher templates ------------------------------------------------------

QUERY_DIGEST_CANDIDATES = """
MATCH (d:Dossier {user_id: $user_id, status: 'ready_to_send'})-[:DOSSIER_FOR]->(p:Person)
OPTIONAL MATCH (c:ConvergenceEvent {user_id: $user_id})-[:ABOUT]->(p)
WITH d, p, coalesce(max(c.score), 0.0) AS max_score
WHERE d.classification IN $classifications
  AND coalesce(d.confidence, 0.0) >= $min_confidence
  AND max_score >= $min_score
RETURN d.id                   AS id,
       d.target_person_id     AS target_person_id,
       coalesce(p.display_name, '') AS target_name,
       d.classification       AS classification,
       coalesce(d.confidence, 0.0) AS confidence,
       max_score              AS score,
       coalesce(d.narrative, '') AS narrative,
       coalesce(d.recommended_action, '') AS recommended_action,
       coalesce(d.key_signals_json, '[]') AS key_signals_json,
       coalesce(d.evidence_bundle_json, '{}') AS evidence_bundle_json
ORDER BY max_score DESC, d.confidence DESC
LIMIT $cap
"""

FLIP_DOSSIER_STATUS_SENT = """
MATCH (d:Dossier {id: $dossier_id})
WITH d, d.status AS prior_status
SET d.status = CASE WHEN prior_status = 'ready_to_send' THEN 'sent' ELSE prior_status END,
    d.status_updated_at = datetime($now_iso),
    d.last_emailed_at   = datetime($now_iso)
RETURN d.status AS new_status, prior_status
"""


# --- Result types ----------------------------------------------------------

@dataclass(frozen=True)
class DigestItem:
    dossier_id: str
    target_name: str
    classification: str
    confidence: float
    score: float
    narrative: str
    recommended_action: str
    key_signals: list[dict[str, str]]
    cross_platform_links: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SendResult:
    sent: bool
    skipped_reason: str | None = None       # 'no_email' | 'disabled' | 'no_candidates' | 'send_failed'
    dossier_count: int = 0
    item_summaries: list[str] = field(default_factory=list)
    sent_at: str | None = None
    provider_message_id: str | None = None


# --- Resend client interface ----------------------------------------------

class EmailClient(Protocol):
    def send(
        self,
        *,
        to: str,
        from_email: str,
        from_name: str,
        subject: str,
        html: str,
    ) -> dict[str, Any]: ...


class ResendClient:
    """Default `EmailClient` using the Resend SDK (lazy-imported so tests don't
    need the package installed)."""

    def __init__(self, api_key: str | None = None):
        api_key = api_key or os.environ.get("RESEND_API_KEY")
        if not api_key:
            raise RuntimeError("RESEND_API_KEY not set in environment")
        try:
            import resend  # type: ignore
        except ImportError as e:
            raise RuntimeError("resend SDK not installed. pip install resend") from e
        resend.api_key = api_key
        self._resend = resend

    def send(self, *, to: str, from_email: str, from_name: str,
             subject: str, html: str) -> dict[str, Any]:
        params = {
            "from": f"{from_name} <{from_email}>",
            "to": [to],
            "subject": subject,
            "html": html,
        }
        return self._resend.Emails.send(params)


# --- Pure-function helpers (no Neo4j, no SDK — fully testable) ------------

def _parse_key_signals(blob: str) -> list[dict[str, str]]:
    try:
        data = json.loads(blob or "[]")
    except json.JSONDecodeError:
        return []
    out: list[dict[str, str]] = []
    if isinstance(data, list):
        for ks in data:
            if isinstance(ks, dict):
                out.append({
                    "claim": str(ks.get("claim", ""))[:200],
                    "supporting_url": str(ks.get("supporting_url", ""))[:300],
                })
    return out


def _row_to_item(row: dict[str, Any]) -> DigestItem:
    return DigestItem(
        dossier_id=row["id"],
        target_name=row.get("target_name") or "",
        classification=row.get("classification") or "unclear",
        confidence=float(row.get("confidence") or 0.0),
        score=float(row.get("score") or 0.0),
        narrative=(row.get("narrative") or "").strip(),
        recommended_action=(row.get("recommended_action") or "").strip(),
        key_signals=_parse_key_signals(row.get("key_signals_json") or "[]"),
    )


def _classification_color(role: str) -> str:
    return {
        "founder":      "#10b981",
        "investor":     "#3b82f6",
        "operator":     "#8b5cf6",
        "unclear":      "#6b7280",
        "not_relevant": "#ef4444",
    }.get(role, "#6b7280")


def render_html(
    items: list[DigestItem],
    *,
    web_app_url: str | None,
    overflow_count: int = 0,
    digest_date: datetime | None = None,
) -> str:
    """Pure-function HTML email body. No external deps, no I/O."""
    digest_date = digest_date or datetime.now(timezone.utc)
    date_str = digest_date.strftime("%B %d, %Y")
    lead = items[0] if items else None
    intro = (
        f"<p style='font-size:16px;color:#111;margin:0 0 24px'>"
        f"Your network converged on <strong>{len(items)}</strong> "
        f"high-signal target{'s' if len(items) != 1 else ''} today."
        + (f" Top pick: <strong>{_html_escape(lead.target_name)}</strong>." if lead else "")
        + "</p>"
    )

    cards: list[str] = []
    for it in items:
        color = _classification_color(it.classification)
        signals_html = ""
        if it.key_signals:
            bullets = "".join(
                f"<li style='margin:4px 0'>"
                f"<span>{_html_escape(ks['claim'])}</span> "
                + (f"<a href='{_html_escape(ks['supporting_url'])}' style='color:#3b82f6;text-decoration:none'>→ evidence</a>"
                   if ks.get("supporting_url") else "")
                + "</li>"
                for ks in it.key_signals[:3]
            )
            signals_html = (
                f"<ul style='margin:12px 0;padding-left:18px;font-size:14px;color:#374151'>{bullets}</ul>"
            )
        action_html = ""
        if it.recommended_action:
            action_html = (
                f"<p style='margin:12px 0 0;font-size:13px;color:#6b7280'>"
                f"<strong style='color:#111'>Recommended action:</strong> "
                f"{_html_escape(it.recommended_action)}</p>"
            )
        dossier_link = (
            f"{web_app_url.rstrip('/')}/dossier/{it.dossier_id}"
            if web_app_url else f"#dossier-{it.dossier_id}"
        )
        cards.append(f"""
<div style='border:1px solid #e5e7eb;border-radius:8px;padding:18px;margin:16px 0;background:#ffffff'>
  <div style='display:flex;align-items:baseline;gap:10px;margin-bottom:6px'>
    <span style='font-size:18px;font-weight:600;color:#111'>{_html_escape(it.target_name)}</span>
    <span style='display:inline-block;padding:2px 8px;border-radius:4px;background:{color};color:#fff;font-size:11px;text-transform:uppercase;font-weight:600'>{_html_escape(it.classification)}</span>
  </div>
  <div style='font-size:12px;color:#6b7280;margin-bottom:8px'>
    Score {it.score:.1f} &middot; Confidence {it.confidence:.0%}
  </div>
  <p style='font-size:14px;color:#374151;line-height:1.5;margin:8px 0'>{_html_escape(it.narrative)}</p>
  {signals_html}
  {action_html}
  <div style='margin-top:14px'>
    <a href='{_html_escape(dossier_link)}' style='display:inline-block;padding:8px 14px;background:#111;color:#fff;border-radius:6px;text-decoration:none;font-size:13px;font-weight:500'>View full dossier &rarr;</a>
  </div>
</div>
""".strip())

    overflow_html = ""
    if overflow_count > 0 and web_app_url:
        overflow_html = (
            f"<p style='font-size:13px;color:#6b7280;margin:24px 0 8px;text-align:center'>"
            f"+ {overflow_count} more dossiers below your notification threshold. "
            f"<a href='{_html_escape(web_app_url)}/dossiers' style='color:#3b82f6'>View all in the web app &rarr;</a>"
            f"</p>"
        )

    return f"""<!doctype html>
<html><body style='margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif'>
<div style='max-width:640px;margin:0 auto;padding:32px 24px;background:#f9fafb'>
<h1 style='font-size:20px;color:#111;margin:0 0 6px'>Signal Convergence digest</h1>
<p style='font-size:13px;color:#6b7280;margin:0 0 24px'>{date_str}</p>
{intro}
{''.join(cards)}
{overflow_html}
<hr style='border:none;border-top:1px solid #e5e7eb;margin:32px 0 16px'>
<p style='font-size:11px;color:#9ca3af;margin:0;line-height:1.5'>
You're receiving this because you set a notification email in your Signal Convergence settings.
Reply to this email or change your settings in the web app to adjust frequency or thresholds.
</p>
</div></body></html>
"""


def _html_escape(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def build_subject(items: list[DigestItem], digest_date: datetime | None = None) -> str:
    digest_date = digest_date or datetime.now(timezone.utc)
    date_str = digest_date.strftime("%b %d")
    if not items:
        return f"Signal Convergence digest — {date_str} (no new leads today)"
    n = len(items)
    label = "new founder lead" if n == 1 else "new founder leads"
    lead = items[0]
    return f"{n} {label} in your network — {date_str} (top: {lead.target_name})"


# --- Main entry point -----------------------------------------------------

def fetch_digest_candidates(
    session,
    *,
    user_id: str,
    rule: AlertRule,
) -> list[DigestItem]:
    """Pull the next-batch ready_to_send dossiers matching the rule's
    notification thresholds."""
    rows = session.run(
        QUERY_DIGEST_CANDIDATES,
        user_id=user_id,
        classifications=list(rule.notify_classifications),
        min_confidence=float(rule.notify_min_confidence),
        min_score=float(rule.notify_min_score),
        cap=int(rule.notify_daily_cap),
    ).data()
    return [_row_to_item(dict(r)) for r in rows]


def send_daily_digest(
    session,
    *,
    user_id: str = "demo",
    rule: AlertRule | None = None,
    email_client: EmailClient | None = None,
    web_app_url: str | None = None,
    from_email: str | None = None,
    from_name: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> SendResult:
    """Send the daily digest if the rule allows. Idempotent at the dossier
    level via the status flip — already-sent dossiers don't appear in the
    candidate query.

    `dry_run=True` runs the query and renders the HTML but does NOT call the
    email client and does NOT flip statuses. Useful for previews.
    """
    rule = rule or get_rule(user_id)
    now = now or datetime.now(timezone.utc)

    if not rule.notify_enabled:
        return SendResult(sent=False, skipped_reason="disabled")
    if not rule.notify_email:
        return SendResult(sent=False, skipped_reason="no_email")

    items = fetch_digest_candidates(session, user_id=user_id, rule=rule)
    if not items:
        return SendResult(sent=False, skipped_reason="no_candidates")

    web_app_url = web_app_url or os.environ.get("FRONTEND_URL", "")
    from_email = from_email or os.environ.get("NOTIFY_FROM_EMAIL", "onboarding@resend.dev")
    from_name = from_name or os.environ.get("NOTIFY_FROM_NAME", "Signal Convergence")

    html = render_html(
        items, web_app_url=web_app_url, digest_date=now,
        overflow_count=0,  # could compute via a second query; keep simple
    )
    subject = build_subject(items, digest_date=now)

    if dry_run:
        return SendResult(
            sent=False,
            skipped_reason=None,
            dossier_count=len(items),
            item_summaries=[
                f"{it.score:.1f} | {it.classification} | {it.target_name}" for it in items
            ],
            sent_at=None,
            provider_message_id="(dry-run)",
        )

    client = email_client or ResendClient()

    try:
        resp = client.send(
            to=rule.notify_email,
            from_email=from_email,
            from_name=from_name,
            subject=subject,
            html=html,
        )
    except Exception as e:
        logger.exception("digest send failed (preserving status): %s", e)
        return SendResult(
            sent=False,
            skipped_reason="send_failed",
            dossier_count=len(items),
            item_summaries=[f"{it.target_name} ({it.score:.1f})" for it in items],
        )

    # Flip each dossier to status='sent' AFTER successful send. If the email
    # provider returned non-2xx we'd have raised above, so reaching here means
    # success.
    for it in items:
        session.run(
            FLIP_DOSSIER_STATUS_SENT,
            dossier_id=it.dossier_id,
            now_iso=now.isoformat(),
        )

    msg_id = ""
    if isinstance(resp, dict):
        msg_id = str(resp.get("id") or resp.get("message_id") or "")

    return SendResult(
        sent=True,
        dossier_count=len(items),
        item_summaries=[f"{it.target_name} ({it.classification}, {it.score:.1f})" for it in items],
        sent_at=now.isoformat(),
        provider_message_id=msg_id,
    )
