"""Provider-agnostic client interfaces for external scraping services.

The TwitterFollowingClient Protocol lets us swap Scrapebadger for twscrape (or
the X API) by changing a single import in scripts/track_scrapebadger_twitter_follows.py.
"""
