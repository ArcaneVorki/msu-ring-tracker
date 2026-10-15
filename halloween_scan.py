#!/usr/bin/env python3
"""
MSU Marketplace watcher: Halloween items

Runs every 15 minutes (GitHub Actions). Each run:
  1. For each name in ITEM_NAMES, POSTs the explore/items endpoint (all pages) and
     keeps listings whose name matches (see name_matches).
  2. GETs /items/{tokenId} for each listing not seen before and reads
     item.timedOption. Results are cached in data/halloween_cache.json so each
     token is fetched only once while listed.
  3. Posts to Discord (webhook secret DISCORD_HALLOWEEN) for each listing where ANY
     stat in WANTED_STATS reaches its minimum, once per listing, with a link to
     the marketplace page.

Environment:
    DISCORD_HALLOWEEN   Discord webhook URL (repo secret). Missing = no alerts.

Reuses the throttle (2s between requests), retries and Discord sender from scan.py.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import scan  # same folder: request_with_retries, post_discord, throttle, read/write helpers

ITEM_NAMES = [
    "Pumpkin Hat",
    "Maroon Chinese Undead Hat",
    "Ghost Fedora",
    "Halloween Cat-O-Lantern Mask",
    "Evil Watermelon Hat",
    "Pumpkin Suit",
    "Maroon Chinese Undead Costume",
    "Halloween Festival",
    "Pumpkin Bat Outfit",
    "Halloween Teddy",
    "Undead Teddy",
    "Halloween Festival Lightbulb",
    "Cat Balloon Cape",
]

# A listing alerts if ANY of these timedOption fields is >= its minimum.
# API field -> (label shown in Discord, minimum)
WANTED_STATS = {
    "imdr": ("Ignored DEF (%)", 10),
    "bdr": ("Boss Damage (%)", 5),
    "pad": ("ATT", 10),
    "allStat": ("All Stats", 20),
}
MAX_PAGES = 10           # safety cap on list pagination per item name
PAGE_SIZE = 135

# Same request shape as the ring scan, with only the name filter that applies here.
# (Ring-only filters such as starforce/potential are left out.)
PAYLOAD = {
    "filter": {
        "name": "",  # set per item name
        "price": {"min": "0", "max": "30000000000"},
    },
    "sorting": "ExploreSorting_RECENTLY_LISTED",
    "walletAddr": "0x212497f6002Cfd5eC0936CaBDd1242389D86bB84",
    "paginationParam": {"pageNo": 1, "pageSize": PAGE_SIZE},
}

DATA_DIR = scan.ROOT / "data"
CACHE_FILE = DATA_DIR / "halloween_cache.json"        # {tokenId: {imdr, expire_at, checked_at}}
NOTIFIED_FILE = DATA_DIR / "halloween_notified.json"  # [tokenId, ...]

log = logging.getLogger("msu-halloween")


def name_matches(listing_name: str, wanted: str) -> bool:
    """Exact name, or the name plus a single-letter variant ("Halloween Festival A")."""
    n = (listing_name or "").strip().casefold()
    w = wanted.casefold()
    return n == w or (n.startswith(w + " ") and len(n) == len(w) + 2 and n[-1].isalpha())


def fetch_listings(item_name: str) -> list[dict]:
    """All listings for one item name, following pagination."""
    out, page = [], 1
    while page <= MAX_PAGES:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["filter"]["name"] = item_name
        payload["paginationParam"]["pageNo"] = page
        resp = scan.request_with_retries("POST", scan.LIST_URL, json=payload)
        out.extend(resp.get("items", []))
        if resp.get("paginationResult", {}).get("isLastPage", True):
            break
        page += 1
    # the name filter may be a substring/fuzzy match: keep only the names we want
    return [i for i in out if name_matches(i.get("name"), item_name)]


def read_timed(detail: dict) -> dict:
    """Wanted stats (plus expiry) from item.timedOption."""
    timed = (detail.get("item") or {}).get("timedOption") or {}
    stats = {k: timed[k] for k in WANTED_STATS if isinstance(timed.get(k), (int, float))}
    return {"stats": stats, "expire_at": timed.get("expireAt")}


def hits(info: dict) -> dict:
    """{field: value} for every wanted stat that reaches its minimum."""
    stats = info.get("stats") or {}
    return {k: stats[k] for k, (_, minimum) in WANTED_STATS.items() if stats.get(k, 0) >= minimum}


def discord_payload(listing: dict, info: dict) -> dict:
    token_id = listing["tokenId"]
    name = listing.get("name") or "Item"
    url = scan.ITEM_PAGE_URL.format(token_id=token_id)
    price = int(listing.get("salesInfo", {}).get("priceWei") or 0) / scan.WEI
    expires = scan._epoch(info.get("expire_at"))
    lines = [f"**{WANTED_STATS[k][0]}:** {v:g}" for k, v in hits(info).items()]
    if expires:
        lines.append(f"**Item expires:** <t:{expires}:F> (<t:{expires}:R>)")
    lines.append(f"**Price:** {price:,.0f}")
    lines.append(f"**Seller:** {listing.get('owner', {}).get('nickname') or '-'}")
    return {
        "content": f"{name} found: {url}",
        "embeds": [{
            "title": f"{name} #{listing.get('tokenInfo', {}).get('mintingNo') or '?'}",
            "url": url,
            "description": "\n".join(lines),
            "color": 0xE8590C,
        }],
        "allowed_mentions": {"parse": []},  # seller names must never ping anyone
    }


def run() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    current: dict[str, dict] = {}
    failed_names = []
    for name in ITEM_NAMES:
        try:
            for listing in fetch_listings(name):
                current[listing["tokenId"]] = listing
        except Exception as err:
            log.error("List request failed for %r: %s", name, err)
            failed_names.append(name)
    log.info("Fetched %d listing(s) across %d item names", len(current), len(ITEM_NAMES) - len(failed_names))
    if len(failed_names) == len(ITEM_NAMES):
        raise RuntimeError("every list request failed")  # state untouched

    cache = scan.read_json(CACHE_FILE, {})
    notified = set(scan.read_json(NOTIFIED_FILE, []))

    # fetch details only for listings we haven't checked yet (old-format cache entries are rechecked)
    for token_id in current:
        if isinstance(cache.get(token_id), dict) and "stats" in cache[token_id]:
            continue
        try:
            info = read_timed(scan.fetch_details(token_id))
        except Exception as err:
            log.error("Detail fetch failed for %s: %s", token_id, err)  # retried next run
            continue
        cache[token_id] = {**info, "checked_at": now}
        scan.write_json_atomic(CACHE_FILE, cache)
        log.info("  %s %s %s", token_id, current[token_id].get("name"), info["stats"])

    matches = [t for t in current if hits(cache.get(t, {}))]
    log.info("Matches: %d", len(matches))

    webhook = os.environ.get("DISCORD_HALLOWEEN", "").strip()
    pending = [t for t in matches if t not in notified]
    if not webhook:
        log.info("DISCORD_HALLOWEEN not set; skipping alerts (%d pending)", len(pending))
    else:
        for t in pending:
            if scan.post_discord(webhook, discord_payload(current[t], cache[t])):
                notified.add(t)
                log.info("Discord: alerted %s", t)
            else:
                log.error("Discord: failed for %s; will retry next run", t)  # not recorded
            scan.time.sleep(1)

    if failed_names:
        # some listings are unknown this run: keep old state so nothing is dropped and re-alerted
        scan.write_json_atomic(CACHE_FILE, cache)
        scan.write_json_atomic(NOTIFIED_FILE, sorted(notified))
        raise RuntimeError(f"list request failed for: {', '.join(failed_names)}")

    # forget tokens no longer listed, so a relisting is checked and alerted again
    scan.write_json_atomic(CACHE_FILE, {t: v for t, v in cache.items() if t in current})
    scan.write_json_atomic(NOTIFIED_FILE, sorted(notified & set(current)))


if __name__ == "__main__":
    try:
        run()
    except Exception as err:
        log.error("Halloween scan failed: %s", err)
        sys.exit(1)
