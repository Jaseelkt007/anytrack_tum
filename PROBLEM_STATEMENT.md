# Social Sourcing for VCs — Problem Statement

*Validated through direct interview with Omar, a German VC who actively pays for Evertrace and has tested Specter.*

---

## TL;DR

A practicing VC who pays for the leading tools in this category (Evertrace, Specter, LinkedIn Sales Navigator) explicitly told us:

> *"If you can do this, I'm 100% certain people will pay you... If you build another Evertrace with that feature on top, then you can just go take the whole market."*

The "feature" is **detecting when multiple trusted angels and investors converge on a founder** — across Twitter, LinkedIn, and GitHub — before that founder publicly raises. No existing tool does this reliably. Specter claims to and fabricates results. Evertrace doesn't attempt it.

This document captures the problem, the validated requirements, the concrete cases we must catch, and the explicit out-of-scope items.

---

## The Core Problem (in Omar's words)

The most painful failure mode for a VC isn't passing on a deal — it's **never seeing it at all** because the signals were happening in someone else's network. Omar described losing deal after deal where, in retrospect, his *own* trusted contacts were already connected to the founder weeks before the round closed. He just had no system to surface that convergence.

His current alternative is manual: open LinkedIn Sales Navigator, scroll through who his trusted contacts are connected to, guess whether the connection is meaningful, repeat for each contact. He called this *"a waste of so much time."*

His one breakout investment — a unicorn within 18 months — happened because an angel manually emailed him about a founder. His own words:

> *"What if he had forgotten? The day he wouldn't send me the email, he forgot."*

The entire current process depends on serendipity and human memory. The opportunity is to systematize it.

---

## Validated Requirement — Priority 1 (the core build)

### 1. Convergence detection across the trusted graph

When **N investors from the user's configured watchlist** independently start engaging with the **same founder** within a sliding time window, fire a signal.

"Engagement" must be concrete and verifiable, not inferred:

- Twitter: follow, reply, mention, retweet
- LinkedIn: new connection, profile mention
- GitHub: starring a repo, following a user

Example trigger condition (from the interview):
> *"Let's say there are like 10 angels that I really like and then I noticed that three of them is following a guy on Twitter or they are connected with a guy on LinkedIn... that's a strong signal."*

This is the entire product in one sentence. Everything else is supporting infrastructure.

### 2. Configurable watchlist (user-provided)

The user provides their own list of trusted investors/angels/operators. We do **not** try to auto-curate the "right" list. Direct quote:

> *"Did you say, do we have the list of VCs or something? You don't need to. I would provide it to you as an input."*

Input shape:
- LinkedIn profile URLs / handles
- Twitter / X handles
- GitHub usernames
- Optional grouping: "trusted angels", "Swedish VCs", "AI investors", etc.

### 3. GitHub-as-social-signal layer

This is a Priority 1 feature that **Evertrace and Specter both miss**. Direct quote:

> *"For open source software... when someone who's like very high value starts following a repo or starting a repo... if I had tracked every repo that's like the founder of Hugging Face and Andrej Karpathy stars and just been less than blind, I would have made an exceptional return."*

So GitHub stars and follows from high-signal accounts (other founders, technical investors, named operators) need to be a first-class signal type, not an afterthought.

### 4. Notification delivery

Simple. Direct quote:
> *"If you can give me this piece of information, I will open your web app or you send me an email. Whatever you want."*

So: email + web app dashboard. No need for Slack / WhatsApp / SMS in v1.

---

## Concrete Cases the Tool Must Catch (Demo Targets)

These are the deals Omar lost that our system should have flagged. Use these as backtest cases for the demo.

### Case 1: Lovable (formerly GPT Engineer)

**Founder profile:**
- Swedish founder, third startup attempt
- Previously worked at Sana (Swedish AI company)
- Two prior failed startups

**Public signals available before announcement:**
- Released open-source project "GPT Engineer" → grew to 30K GitHub stars
- Multiple Swedish VCs in Omar's LinkedIn network connected to the founder
- The deal happened entirely inside the Swedish VC network

**Direct quote on what would have helped:**
> *"He actually first published GPT Engineer. And it got like 30K stars. Maybe you could have told me that... And then you tell me, hey, by the way, Omar, five of your VC friends are already connected with that guy. Reach out. I would have had a chance to get this deal."*

### Case 2: Legora (YC company)

**Founder profile:**
- Y Combinator alum
- Eventually backed by Benchmark

**Public signals available before announcement:**
- Founder's LinkedIn showed new connection with Benchmark partners
- YC batch listing (low signal alone — YC has ~200 companies per batch)

**Direct quote:**
> *"Maybe someone will tell me, hey, by the way, this guy is connected with Benchmark now. Maybe something is happening. That's why the social signal is very important."*

### Case 3: The unicorn that came from one email

His best investment — invested → became a unicorn 18 months later — came from an angel investor manually emailing him: *"I know this guy for a while. He's going to raise. You want to chat with him."*

The angel discovered the founder through a community called **Sigma Squared** (network of entrepreneurs under 27).

**Implication for the build:** systemize what currently happens by chance. If we'd been monitoring that angel's connections, we would have surfaced the founder *before* the email — and not relied on the angel remembering to write.

---

## Why Existing Tools Don't Solve This

### Specter — actively unreliable

The VC tested Specter directly and found it fabricates engagement data. Direct quotes:

> *"They send me shit, man. I actually tried to test it. I started asking founders I know, hey, did you really talk to Sequoia? Like, no, man... I tried to test it multiple times and it just didn't work."*

> *"They tell me, for example, hey, Omar, you should take a look at this guy. He is talking with Sequoia right now. I'm like, ah, okay, great, let me talk. And then like, no, it didn't happen... Hey, this guy is actually talking with founders' phone. And then I'm like, we never know this guy. So it was just like fake information."*

**Failure mode**: vague definition of "engagement" + inference from indirect public signals → high false-positive rate → user trust collapses.

**Implication for our build**: every signal we surface must be **directly observable** (a star, a follow, a connection that a user could click and verify) — never inferred or aggregated from secondary sources.

### Evertrace — solid foundation but no convergence layer

The VC pays for Evertrace and considers it useful. But it does not solve this specific problem.

> *"EverTrace doesn't do the social thing. EverTrace doesn't do GitHub. These are the two things I could think of. They don't... I don't see if someone is following someone on Twitter. They don't have this. They only show when people change their headlines or something. They do it on LinkedIn."*

**Implication**: we should position as a complement to Evertrace, not a replacement. Pitch line:

> *"Evertrace tells you who exists. We tell you which ones your trusted network is converging on."*

---

## Explicitly Out of Scope for v1 (his guidance)

The VC was clear about what is *not* the priority. We should resist scope creep on these:

| Feature | His take |
|---|---|
| **Hackathon winner detection** | *"Interesting to know, but how many of them will build a company? It's a metric, just one piece of it."* |
| **PhD / academic founder detection** | *"It's a signal, but it's not the highest priority signal for me."* |
| **Vesting schedule monitoring** | *"Interesting idea, but EverTrace does not do it"* — acknowledged as clever but not P1 |
| **Geographic coverage analysis** ("you don't have enough coverage in Sweden") | *"Step number two. Solve the basic problem first."* |
| **Sector coverage analysis** ("you don't have enough coverage in robotics") | *"Step number two."* |
| **Auto-recommended people to connect with** | *"Very good idea, but step number two."* |
| **Track record / founder credentials** | *"It doesn't matter. I just want to see who's about to fundraise."* |

These are real opportunities for v2/v3 — but the demo and MVP should ignore them entirely.

---

## Target User Profile

Based on Omar specifically, but generalizable to the broader buyer persona:

- **Role**: Partner / investor at an early-stage fund (seed / pre-seed / Series A)
- **Geography**: Based in one country (Germany), loses deals in adjacent geographies (Sweden, UK) where he has weaker network density
- **Existing tool stack**: Evertrace (paid), Specter (tested, abandoned), LinkedIn Sales Navigator (manual, frustrating)
- **Network**: Hundreds of LinkedIn connections to angels, micro-VC GPs, and operator-investors
- **Workflow pain**: Manual check of "who is X connected to that I'm not?" via Sales Navigator — described as *"a waste of so much time"*
- **Decision priority**: Angel investors > smaller VCs > big VCs (in that order of signal value)

Direct quote on angel investor priority:
> *"Angel investors, 100%. Angel investors, smaller VCs, even good VCs. They come even before VCs."*

---

## Pricing Signal

The VC didn't quote a number, but the tone was clear:

> *"I'm 100% certain people will pay you... VCs would be willing to pay good money for it."*

Reasonable expectation: same range as Evertrace, which is enterprise SaaS for VCs, likely **$10K–$50K per fund per year**, possibly higher for API access.

---

## Top Quotes for the Demo Pitch

The single best ones, in order of usefulness:

1. **"If you can do this, I'm 100% certain people will pay you."** — direct demand validation
2. **"Specter tried to build that, but they couldn't... it was just like fake information."** — competitive moat (we do what they can't)
3. **"Five of your VC friends are already connected with that guy. Reach out. I would have had a chance to get this deal."** — the exact use case in the user's own words
4. **"Angel investors, 100%. They come even before VCs."** — validates the focus on the angel layer
5. **"Build another Evertrace with that feature on top, then you can just go take the whole market."** — positioning at the right altitude

---

## What Our Build Must Prove (Definition of Done for the Demo)

A successful demo must concretely show:

1. **The convergence signal is real and detectable** — not fabricated. Show real Twitter follows, real LinkedIn connections, real GitHub stars, with timestamps and verifiable links.
2. **The watchlist is configurable** — show the user uploading a list of investor handles and the system immediately tracking them.
3. **A retroactive case lands** — run the system against historical data for Lovable or a comparable founder, show that we'd have flagged them weeks before the public announcement, and identify the specific converging investors.
4. **No false positives** — the demo must include a "confidence" or "evidence" view showing the user can click through every claimed signal to its source.

---

## Strategic Positioning (one-sentence pitch)

> *"Evertrace tells you which founders exist. Specter claims to tell you who's about to raise but fabricates the data. We do what Specter promised — detect when your own trusted network of angels and investors converges on a founder — using only directly observable signals from Twitter, LinkedIn, and GitHub."*

---

## Next Actions for the Team

1. **P1 (Data)**: Set up Twitter, LinkedIn, and GitHub scrapers parameterized to take a watchlist of handles as input. Same scraper must support historical reconstruction (for Case 1 and Case 2 backtests).
2. **P2 (Intelligence)**: Define the convergence detection algorithm. Specifically: when ≥N watchlist members all engage with the same target person within window W, fire signal. Tune N and W against the Lovable case.
3. **P3 (Backtest)**: Reconstruct the public social graph around the Lovable founder before the round announcement. Confirm the system catches it. Repeat for one more case.
4. **P4 (Application)**: Watchlist upload UI. Founder detail page that shows *which* watchlist members converged, *when*, and links to verify each engagement directly.

The bar to clear: every signal a clickable, verifiable link. No black boxes. No "Sequoia is interested" without a concrete tweet, follow, or connection to back it up.
