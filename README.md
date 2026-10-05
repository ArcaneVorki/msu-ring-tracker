# Ring Watch

Scans the MSU Marketplace for Noble Ifia's Ring every 6 hours with a GitHub Actions workflow, and publishes the rings that match your conditions to a GitHub Pages site. Each match shows its stats and links to `https://msu.io/marketplace/nft/<tokenId>`.

## Setup

1. Create a GitHub repo and push these files to the `main` branch.
2. **Settings > Pages > Build and deployment > Source: GitHub Actions.**
3. **Actions > Scan marketplace > Run workflow** to do the first scan. After that it runs on its own every 6 hours.

The site will be at `https://<your-username>.github.io/<repo-name>/`.

## Discord alerts (optional)

1. In your Discord server: **Channel settings > Integrations > Webhooks > New Webhook**, then copy the webhook URL.
2. In the repo: **Settings > Secrets and variables > Actions > New repository secret**
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: the webhook URL (treat it like a password: anyone with it can post to your channel).
3. That's it. Each scan posts any matching ring it hasn't alerted you about yet, with stats and a link to the item.

Alerts are tracked in `data/notified.json`, so a ring that stays listed only alerts once. A ring that is delisted and later relisted alerts again. If a send fails it is retried on the next scan. Without the secret, scans run normally with no alerts, and rings that match in the meantime alert once you add it.

## How it works

- `scan.py` calls the list endpoint, then the item detail endpoint for each listing. Details are cached in `data/details.json` and only re-fetched when older than 7 days.
- Matches are written to `docs/data.json`, which `docs/index.html` renders.
- The workflow commits the `data/` cache back to the repo (so the 7-day cache survives between runs) and deploys `docs/` to Pages.
- `data/history.csv` gets one row per listing per scan.

## Match conditions

Edit them at the top of `scan.py`.

- **Potential:** all 3 lines are the same one of `STR: +12%`, `INT: +12%`, `LUK: +12%`, `DEX: +12%`, `All Stats: +9%`.
- **Bonus potential:** per stat family (DEX, STR, LUK use `ATT`; INT uses `Magic ATT`), at least 2 lines from `<STAT>: +7%`, `<STAT> per 10 Character Levels: +2`, `<attack>: +14` (duplicates count), and the remaining line is one of those, an `All Stats` line, the family's attack line, or `<STAT>: N%`. Flat stats like `DEX: +6` never count.

## Changing the schedule

Edit the `cron` line in `.github/workflows/scan.yml`. GitHub cron runs in UTC and can start a few minutes late.

## Notes

- The Pages site is public. Anyone with the link can see the matches.
- If scans start failing with 403 errors, msu.io is probably blocking GitHub's runner IPs. Check the **Run scan** step log.
- GitHub pauses scheduled workflows in repos with no activity for 60 days. The cache commits normally keep it active, but re-enable it from the Actions tab if it ever stops.
