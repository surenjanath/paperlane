# paperlane.insightfusiontech.com

The website for [Paperlane](https://github.com/surenjanath/paperlane). This branch holds only the static site:
plain HTML, CSS and a little JavaScript, with no build step.

| File | Purpose |
|---|---|
| `index.html` | The page |
| `assets/site.css`, `assets/site.js` | Styles; copy buttons, tabs, menu, scroll reveal, GitHub star count |
| `assets/img/` | Screenshots taken from the app's demo data (`seed_demo`, `seed_stories`, `seed_bank_demo`) |
| `_headers` | Cloudflare Pages security and cache headers |
| `404.html`, `robots.txt`, `sitemap.xml` | Served as-is by Cloudflare Pages |

## Preview locally

```bash
python3 -m http.server 8080   # then open http://localhost:8080
```

## Deploy (Cloudflare Pages)

Workers & Pages → Create → Pages → Connect to Git → `surenjanath/paperlane`

- Production branch: `website`
- Framework preset: None · Build command: *(empty)* · Build output directory: `/`

Then Custom domains → `paperlane.insightfusiontech.com`. Every push to `website` redeploys.

When you change `assets/site.css` or `assets/site.js`, bump the `?v=` number where `index.html` and `404.html` load them; assets are cached for a day.
