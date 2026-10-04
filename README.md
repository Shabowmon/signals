# Signals

Robert's money-verdict timeline: podcast episodes classified as
**Opportunity / Risk / Theme / None**, newest first, filterable by source and verdict.

v1 is a static site (same pattern as the lifts and recipes apps): plain
HTML/CSS/JS, no build step, no backend. Episode data lives in
`episodes.json` and is rendered by `app.js`. A scheduled agent run
regenerates `episodes.json` from new transcripts and pushes to `main`,
which auto-deploys.

## What's inside

- `index.html` — page shell (header, filters, stats, timeline, footer)
- `styles.css` — dark minimal theme, phone-friendly
- `app.js` — fetches `episodes.json`, renders filter chips, stats, and cards
- `episodes.json` — 36 seed episodes (Planet Money 21 · Up First 5 · Odd Lots 10),
  reverse-chronological. Carries a leading `// generated <date>` comment line
  (stripped by `app.js` before parsing) plus the build date in the footer.

## Deploy (same dance as lifts/recipes)

On Fedora:

```bash
cd ~/Projects/web
# download signals.zip from the chat panel, then:
unzip ~/Downloads/signals.zip -d signals
cd signals
git init -b main
git add -A
git commit -m "Signals v1"
gh repo create Shabowmon/signals --public --source=. --push
```

Then in Cloudflare (Workers & Pages → Create an app):
1. Select the `Shabowmon/signals` repository.
2. Project name: `signals`.
3. Build command: *(leave empty)*.
4. Deploy command: `npx wrangler deploy`.
5. Deploy, then add custom domain `signals.robertnaanos.com`
   (Worker → Domains → Add Domain).

Pushes to `main` auto-deploy. If the new subdomain doesn't resolve on the
iPhone at first, airplane-mode toggle.
