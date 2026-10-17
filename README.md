# Life Wiki Dashboard

A password-protected dashboard over a Notion Life Wiki (Active Projects, Impossible List, Task Planner, Life Principles) plus a Skill Tree export. Hosted on GitHub Pages; the data file is AES-GCM encrypted, so the public site shows nothing without the password.

A GitHub Action pulls Notion every 3 hours, rebuilds `data.enc` and redeploys. An open tab checks for a newer sync every 10 minutes.

## Setup

1. **Notion integration**: go to <https://www.notion.so/profile/integrations>, create an internal integration with *Read content* only, and copy its secret. Then open the **Life Wiki** page in Notion → `•••` → **Connections** → add the integration (it inherits access to the databases inside).
2. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `NOTION_TOKEN`: the integration secret
   - `DASHBOARD_PASSWORD`: the password the page asks for
   - `SKILLTREE_JSON`: the Skill Tree app's JSON export (optional)
3. **Pages**: Settings → Pages → Source: *GitHub Actions*.
4. Run the workflow (Actions → *Sync Notion and publish* → *Run workflow*).

## Updating the Skill Tree

Export from the Skill Tree app, then:

```bash
gh secret set SKILLTREE_JSON < skilltree-YYYY-MM-DD.json
gh workflow run sync.yml
```

## Local build

```bash
pip install -r requirements.txt
DASHBOARD_PASSWORD=test python build.py site --sample   # sample data, no Notion call
python -m http.server -d site 8000
```

`web-src/dashboard.html` is the dashboard page (the same code as the claude.ai version); `shell-head.html` supplies its theme tokens and a small stand-in for the claude.ai dashboard runtime, `shell-tail.html` the unlock and refresh logic.
