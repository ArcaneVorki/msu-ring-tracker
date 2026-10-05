#!/usr/bin/env python3
"""
MSU Marketplace scanner: Noble Ifia's Ring

One scan per run (GitHub Actions runs it every 6 hours):
  1. POST the explore/items endpoint and collect current listings.
  2. For each listing, GET /items/{tokenId} for stats / starforce / potential /
     bonusPotential. Details are cached in data/details.json and only re-fetched
     when the cached copy is older than 7 days.
  3. Evaluate the match conditions and write docs/data.json for the website.
  4. Post each newly matching ring to Discord (once per ring).

Environment (both optional):
    DISCORD_WEBHOOK_URL   Discord webhook to notify (repo secret). No webhook = no alerts.
    SITE_URL              link to the Pages site, included in the alert message

Usage:
    pip install requests
    python scan.py
"""

import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LIST_URL = "https://msu.io/marketplace/api/marketplace/explore/items"
DETAIL_URL = "https://msu.io/marketplace/api/marketplace/items/{token_id}"
ITEM_PAGE_URL = "https://msu.io/marketplace/nft/{token_id}"

PAYLOAD = {
    "filter": {
        "name": "Noble Ifia's Ring",
        "price": {"min": "0", "max": "30000000000"},
        "level": {"min": 0, "max": 275},
        "starforce": {"min": 0, "max": 25},
        "potential": {"min": 0, "max": 4},
        "bonusPotential": {"min": 4, "max": 4},
    },
    "sorting": "ExploreSorting_RECENTLY_LISTED",
    "walletAddr": "0x212497f6002Cfd5eC0936CaBDd1242389D86bB84",
    "paginationParam": {"pageNo": 1, "pageSize": 135},
}

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://msu.io",
    "Referer": "https://msu.io/marketplace",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

WEI = 10**18
DETAILS_MAX_AGE = timedelta(days=7)     # re-fetch details older than this
REQUEST_DELAY = 2.0                     # minimum seconds between any two msu.io API requests

# --------------------------------------------------------------------------- #
# Match conditions
# --------------------------------------------------------------------------- #
# Potential: ALL THREE lines equal one of these labels exactly.
TARGET_POTENTIALS = [
    "STR: +12%",
    "INT: +12%",
    "LUK: +12%",
    "DEX: +12%",
    "All Stats: +9%",
]

# Bonus potential, evaluated once per stat "family"; a ring matches if ANY family matches.
# Within one family:
#   - at least 2 of the 3 options are in the family's core set (duplicates count), AND
#   - the remaining option is in the core set too, or is one of:
#       * any "All Stats" line
#       * any attack line for that family (ATT for STR/DEX/LUK, Magic ATT for INT)
#       * "<STAT>: +N%" (percent only; flat "DEX: +6" does NOT count)
# Lines from different families are not mixed.
# (stat, attack label that family uses)
BONUS_FAMILY_DEFS = [
    ("DEX", "ATT"),
    ("STR", "ATT"),
    ("LUK", "ATT"),
    ("INT", "Magic ATT"),
]


def _build_bonus_families() -> dict:
    families = {}
    for stat, att in BONUS_FAMILY_DEFS:
        s, a = stat.lower(), att.lower()
        families[stat] = {
            "core": {
                f"{s}: +7%",
                f"{s} per 10 character levels: +2",
                f"{a}: +14",
            },
            "last": [
                re.compile(r"^all stats\b", re.IGNORECASE),
                re.compile(rf"^{re.escape(a)}:", re.IGNORECASE),            # exact attack label
                re.compile(rf"^{s}:\s*\+?\d+(\.\d+)?%$", re.IGNORECASE),    # stat must be a percent
            ],
        }
    return families


BONUS_FAMILIES = _build_bonus_families()

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
HISTORY_CSV = DATA_DIR / "history.csv"
SEEN_FILE = DATA_DIR / "seen.json"
NOTIFIED_FILE = DATA_DIR / "notified.json"
DETAILS_FILE = DATA_DIR / "details.json"
SITE_DATA_FILE = DOCS_DIR / "data.json"

CSV_FIELDS = [
    "scraped_at", "token_id", "minting_no", "price", "starforce",
    "potential_grade", "bonus_potential_grade", "seller", "seller_wallet",
    "listed_at", "expires_at",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("msu-scan")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
_last_request_at = 0.0


def throttle() -> None:
    """Sleep as needed so msu.io requests (list, details, retries) are >= REQUEST_DELAY apart."""
    global _last_request_at
    wait = REQUEST_DELAY - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def request_with_retries(method: str, url: str, retries: int = 3, backoff: int = 10, **kwargs):
    last_err = None
    for attempt in range(1, retries + 1):
        throttle()
        try:
            resp = requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as err:
            last_err = err
            log.warning("%s %s attempt %d/%d failed: %s", method, url, attempt, retries, err)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"{method} {url} failed after {retries} attempts: {last_err}")


def fetch_items() -> list[dict]:
    return request_with_retries("POST", LIST_URL, json=PAYLOAD).get("items", [])


def fetch_details(token_id: str) -> dict:
    return request_with_retries("GET", DETAIL_URL.format(token_id=token_id))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_item(item: dict) -> dict:
    data = item.get("data", {})
    sales = item.get("salesInfo", {})
    owner = item.get("owner", {})
    price_wei = int(sales.get("priceWei") or 0)
    return {
        "token_id": item.get("tokenId"),
        "minting_no": item.get("tokenInfo", {}).get("mintingNo"),
        "price": price_wei / WEI,
        "starforce": data.get("starforce"),
        "potential_grade": data.get("potentialGrade"),
        "bonus_potential_grade": data.get("bonusPotentialGrade"),
        "seller": owner.get("nickname"),
        "seller_wallet": sales.get("sellerWalletAddr"),
        "listed_at": sales.get("createdAt"),
        "expires_at": sales.get("expiredAt"),
    }


def extract_details(detail: dict) -> dict:
    """Keep only stats / starforce / potential / bonusPotential from the detail response."""
    item = detail.get("item", {})
    enhance = item.get("enhance", {})
    return {
        "stats": item.get("stats"),
        "starforce": enhance.get("starforce"),
        "potential": enhance.get("potential"),
        "bonusPotential": enhance.get("bonusPotential"),
    }


def block_labels(block: dict | None) -> list[str]:
    """['LUK: +12%', 'LUK: +9%', 'LUK: +9%'] from a potential / bonusPotential block."""
    if not block:
        return []
    return [
        (block[k].get("label") or "?")
        for k in ("option1", "option2", "option3")
        if isinstance(block.get(k), dict)
    ]


def matching_target(details: dict | None) -> str | None:
    """Matched target label if all 3 potential lines are the same target, else None."""
    block = (details or {}).get("potential")
    if not block:
        return None
    labels = [block.get(k, {}).get("label") for k in ("option1", "option2", "option3")]
    if len(set(labels)) == 1 and labels[0] in TARGET_POTENTIALS:
        return labels[0]
    return None


def bonus_match_families(details: dict | None) -> list[str]:
    """Stat families (DEX/STR/LUK/INT) whose bonus potential condition the ring satisfies."""
    block = (details or {}).get("bonusPotential")
    if not block:
        return []
    labels = [
        (block.get(k, {}).get("label") or "").strip()
        for k in ("option1", "option2", "option3")
    ]
    if not all(labels):
        return []

    matched = []
    for name, fam in BONUS_FAMILIES.items():
        core_flags = [lbl.casefold() in fam["core"] for lbl in labels]
        core_count = sum(core_flags)
        if core_count == 3:
            matched.append(name)
        elif core_count == 2:
            last = labels[core_flags.index(False)]
            if any(p.match(last) for p in fam["last"]):
                matched.append(name)
    return matched


def stat_totals(details: dict | None) -> dict:
    """Headline stats for the website."""
    stats = (details or {}).get("stats") or {}

    def total(key):
        v = stats.get(key)
        return v.get("total") if isinstance(v, dict) else None

    return {
        "str": total("str"), "dex": total("dex"), "int": total("int"), "luk": total("luk"),
        "max_hp": total("maxHp"), "max_mp": total("maxMp"),
        "pad": total("pad"), "mad": total("mad"), "pdd": total("pdd"),
        "ruc": stats.get("ruc"),
    }


# --------------------------------------------------------------------------- #
# State / cache files
# --------------------------------------------------------------------------- #
def read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting fresh", path.name)
    return default


def write_json_atomic(path: Path, obj, indent: int | None = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def is_fresh(entry: dict | None, now: datetime) -> bool:
    """True if the cached entry was fetched within the last DETAILS_MAX_AGE."""
    if not entry or "fetched_at" not in entry:
        return False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except ValueError:
        return False
    return now - fetched < DETAILS_MAX_AGE


def append_history(rows: list[dict], scraped_at: str) -> None:
    HISTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({"scraped_at": scraped_at, **row})


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
def refresh_details(token_ids: list[str], cache: dict, now: datetime) -> int:
    """GET details for tokens missing from cache or older than 7 days. Returns #fetched."""
    stale = [t for t in token_ids if not is_fresh(cache.get(t), now)]
    log.info("Details: %d cached & fresh, %d to fetch", len(token_ids) - len(stale), len(stale))

    fetched = 0
    for token_id in stale:
        try:
            detail = fetch_details(token_id)
            cache[token_id] = {
                "fetched_at": now.isoformat(timespec="seconds"),
                **extract_details(detail),
            }
            fetched += 1
            write_json_atomic(DETAILS_FILE, cache)  # save as we go so a failure loses nothing
        except Exception as err:
            # keep the old (stale) entry if there is one; we'll retry next scan
            log.error("Detail fetch failed for %s: %s", token_id, err)
    return fetched


# --------------------------------------------------------------------------- #
# Discord notifications
# --------------------------------------------------------------------------- #
DISCORD_EMBEDS_PER_MESSAGE = 10  # Discord's limit


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def discord_embed(m: dict) -> dict:
    st = m.get("stats") or {}

    def n(v):
        return f"{v:,}" if isinstance(v, (int, float)) else "-"

    badges = []
    if m.get("potential_match"):
        badges.append(f"Potential: {m['potential_match']} x3")
    badges += [f"Bonus: {f}" for f in m.get("bonus_families", [])]

    sf = m.get("starforce")
    sf_text = "-" if sf is None else f"{sf}/{m['max_starforce']}" if m.get("max_starforce") else str(sf)
    expires = _epoch(m.get("expires_at"))

    fields = [
        {"name": "Potential", "value": "\n".join(m.get("potential_lines") or ["-"])[:1000], "inline": True},
        {"name": "Bonus potential", "value": "\n".join(m.get("bonus_lines") or ["-"])[:1000], "inline": True},
        {
            "name": f"Stats (starforce {sf_text})",
            "value": (
                f"STR {n(st.get('str'))} · DEX {n(st.get('dex'))} · INT {n(st.get('int'))} · LUK {n(st.get('luk'))}\n"
                f"ATT {n(st.get('pad'))} · M.ATT {n(st.get('mad'))} · HP {n(st.get('max_hp'))} · DEF {n(st.get('pdd'))}"
            ),
            "inline": False,
        },
        {
            "name": "Listing",
            "value": f"Seller {m.get('seller') or '-'}" + (f" · expires <t:{expires}:R>" if expires else ""),
            "inline": False,
        },
    ]
    return {
        "title": f"Noble Ifia's Ring #{m.get('minting_no') or '?'} - {m['price']:,.0f}",
        "url": m["url"],
        "description": " · ".join(badges),
        "color": 0x7A3FF2 if m.get("potential_match") else 0x0F8B6D,
        "fields": fields,
    }


def post_discord(webhook: str, payload: dict, retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(webhook, json=payload, timeout=30)
            if resp.status_code == 429:  # rate limited: wait as told, then retry
                wait = min(float(resp.json().get("retry_after", 2)), 30)
                log.warning("Discord rate limited, waiting %.1fs", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return True
        except (requests.RequestException, ValueError) as err:
            log.warning("Discord post attempt %d/%d failed: %s", attempt, retries, err)
            time.sleep(2 * attempt)
    return False


def notify_discord(webhook: str, matches: list[dict], site_url: str) -> bool:
    """Send matches in batches of 10 embeds. Returns True only if every batch was delivered."""
    ok = True
    for i in range(0, len(matches), DISCORD_EMBEDS_PER_MESSAGE):
        batch = matches[i:i + DISCORD_EMBEDS_PER_MESSAGE]
        head = f"New matching ring{'s' if len(matches) != 1 else ''}: {len(matches)}"
        payload = {
            "content": head + (f" - {site_url}" if site_url and i == 0 else ""),
            "embeds": [discord_embed(m) for m in batch],
            "allowed_mentions": {"parse": []},  # seller names must never ping anyone
        }
        if not post_discord(webhook, payload):
            ok = False
        time.sleep(1)
    return ok


def build_match(row: dict, details: dict, is_new: bool) -> dict:
    sf = (details.get("starforce") or {}) if details else {}
    return {
        "token_id": row["token_id"],
        "url": ITEM_PAGE_URL.format(token_id=row["token_id"]),
        "minting_no": row["minting_no"],
        "price": row["price"],
        "starforce": sf.get("enhanced", row["starforce"]),
        "max_starforce": sf.get("maxStarforce"),
        "seller": row["seller"],
        "listed_at": row["listed_at"],
        "expires_at": row["expires_at"],
        "is_new": is_new,
        "potential_match": matching_target(details),
        "bonus_families": bonus_match_families(details),
        "potential_lines": block_labels((details or {}).get("potential")),
        "bonus_lines": block_labels((details or {}).get("bonusPotential")),
        "stats": stat_totals(details),
        "details_fetched_at": (details or {}).get("fetched_at"),
    }


def run_scan() -> None:
    now = datetime.now(timezone.utc)
    scraped_at = now.isoformat(timespec="seconds")

    # If the list call fails this raises, the job fails, and the previous site data stays up.
    items = fetch_items()
    rows = sorted((parse_item(i) for i in items), key=lambda r: r["price"])

    seen = set(read_json(SEEN_FILE, []))
    current_ids = {r["token_id"] for r in rows}
    first_run = not seen
    new_ids = set() if first_run else current_ids - seen
    log.info("Fetched %d listings (%d new since last scan)", len(rows), len(new_ids))

    cache = read_json(DETAILS_FILE, {})
    refresh_details([r["token_id"] for r in rows], cache, now)

    matches = []
    for r in rows:  # already sorted by price
        details = cache.get(r["token_id"])
        if not details:
            continue
        if matching_target(details) or bonus_match_families(details):
            matches.append(build_match(r, details, r["token_id"] in new_ids))

    log.info(
        "Matches: %d (%d potential, %d bonus)",
        len(matches),
        sum(1 for m in matches if m["potential_match"]),
        sum(1 for m in matches if m["bonus_families"]),
    )
    for m in matches:
        log.info("  %s  price=%s  pot=%s  bonus=%s  %s",
                 m["token_id"], f"{m['price']:,.0f}", m["potential_match"], m["bonus_families"], m["url"])

    write_json_atomic(
        SITE_DATA_FILE,
        {
            "generated_at": scraped_at,
            "total_listings": len(rows),
            "new_listings": len(new_ids),
            "potential_targets": TARGET_POTENTIALS,
            "matches": matches,
        },
        indent=None,
    )

    # ---- Discord: alert once per matching ring ----
    # notified.json holds tokens already alerted. Entries for rings no longer listed are dropped,
    # so a relisted ring alerts again. Failed sends are not recorded and retry next scan.
    notified = set(read_json(NOTIFIED_FILE, [])) & current_ids
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    to_notify = [m for m in matches if m["token_id"] not in notified]
    if not webhook:
        log.info("DISCORD_WEBHOOK_URL not set; skipping notifications (%d pending)", len(to_notify))
    elif to_notify:
        site_url = os.environ.get("SITE_URL", "").strip()
        if notify_discord(webhook, to_notify, site_url):
            notified |= {m["token_id"] for m in to_notify}
            log.info("Discord: notified %d ring(s)", len(to_notify))
        else:
            log.error("Discord: some notifications failed; will retry next scan")
    write_json_atomic(NOTIFIED_FILE, sorted(notified))

    append_history(rows, scraped_at)
    write_json_atomic(SEEN_FILE, sorted(current_ids))  # drop sold/expired so relistings count as new


if __name__ == "__main__":
    try:
        run_scan()
    except Exception as err:
        log.error("Scan failed: %s", err)
        sys.exit(1)
