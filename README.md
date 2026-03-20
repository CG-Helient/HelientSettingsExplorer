# Helient Settings Explorer

A self-hosted, always-fresh policy settings cross-reference tool covering:
- **Intune Settings Catalog** — 10,000+ `settingDefinitionId` values with OMA-URI and JSON fragments
- **Windows GPO / ADMX** — policy paths, registry keys, ADMX file references
- **Microsoft Edge + Google Chrome** — all browser policies (shared Chromium engine)
- **Office 365 / M365 Apps** — macro security, trusted documents, Cloud Policy settings
- **Custom sources** — CSV, Excel, JSON file import + remote URL sources

Built on the same architecture as [intunesettings.app](https://intunesettings.app):
GitHub Actions fetches data weekly → commits pre-built JSON chunks → GitHub Pages serves the static site.
**Zero hosting cost. Zero database to maintain. Always current.**

---

## Quick Start (10 minutes)

### 1. Fork or create this repo

Push all files to a new GitHub repository.

### 2. Enable GitHub Pages

`Settings → Pages → Source: Deploy from branch → Branch: main → Folder: /public`

Your site will be live at `https://<your-username>.github.io/<repo-name>`

### 3. Create an Azure App Registration (for Graph API data)

1. Go to [Azure Portal → App Registrations → New Registration](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps)
2. Name: `Helient Settings Explorer`
3. Supported account types: **Accounts in this organizational directory only**
4. Redirect URI: Add `https://<your-github-pages-url>` as type **Single-page application (SPA)**
5. After creation, go to **API permissions → Add permission → Microsoft Graph → Application permissions**
6. Add: `DeviceManagementConfiguration.Read.All`
7. Click **Grant admin consent**
8. Go to **Certificates & secrets → New client secret** → copy the value immediately

### 4. Add GitHub Secrets

In your repo: `Settings → Secrets and variables → Actions → New repository secret`

| Secret name            | Value                                      |
|------------------------|--------------------------------------------|
| `AZURE_CLIENT_ID`      | Application (client) ID from App Registration |
| `AZURE_CLIENT_SECRET`  | Client secret value you copied             |
| `AZURE_TENANT_ID`      | Your Azure AD tenant ID (or `common`)     |

### 5. Run the initial build

`Actions → Build Settings Index → Run workflow → sources: all`

The workflow will fetch ~10,000+ settings from all sources, build the index, commit to `data/`, and deploy.

### 6. Open your site

Navigate to your GitHub Pages URL. The settings index loads automatically.

---

## Adding Custom Sources

### From the browser (instant)

1. Click **⚙ Settings** in the top-right
2. **Upload file**: drag & drop an Excel (.xlsx), CSV, or JSON file
   - Expected columns: `name`, `description`, `settingDefinitionId`, `registryKey`, `registryValue`, `gpoPath`
3. **Remote JSON URL**: paste a public URL and click **Add**

Custom entries persist in the browser's `localStorage` and are immediately searchable.

### Via the repo (for team-shared sources)

Edit `custom_sources.json` in the repo root:

```json
{
  "sources": [
    {
      "url":  "https://raw.githubusercontent.com/your-org/repo/main/settings.json",
      "type": "json",
      "name": "Our Organization Custom Settings"
    }
  ]
}
```

Commit the file → the next scheduled Action run will include it in the index.

---

## How It Works

```
┌─────────────────────────────────────────────────┐
│           GitHub Actions (weekly cron)           │
│                                                   │
│  scripts/build_index.py                          │
│    ├── Microsoft Graph API  → 10,000+ settings   │
│    ├── Chromium policies    → 500+ browser rules │
│    ├── Windows ADMX         → GPO/registry refs  │
│    ├── Office ADMX          → M365 app policies  │
│    └── custom_sources.json  → your custom data   │
│                  ↓                               │
│         data/meta.json                           │
│         data/chunks/chunk_000.json  (~100 KB)    │
│         data/chunks/chunk_001.json               │
│         ...                                      │
└───────────────────────┬─────────────────────────┘
                        │ git push
                        ↓
┌─────────────────────────────────────────────────┐
│              GitHub Pages (free CDN)             │
│                                                   │
│  public/index.html                               │
│    ├── Loads meta.json on page open              │
│    ├── Lazy-loads chunks in parallel             │
│    ├── Full-text search in browser (no server)  │
│    └── MSAL auth for live Graph API supplement  │
└─────────────────────────────────────────────────┘
```

## Running Locally

```bash
# Install Python (3.9+) — no pip packages required
python scripts/build_index.py --sources admx,office,chromium

# Or with Graph API credentials:
AZURE_CLIENT_ID=xxx AZURE_CLIENT_SECRET=xxx AZURE_TENANT_ID=xxx \
  python scripts/build_index.py --sources all

# Serve the site locally:
cd public && python -m http.server 8080
# Open http://localhost:8080
```

## Repo Structure

```
helient-settings-explorer/
├── .github/
│   └── workflows/
│       └── build-index.yml      # Scheduled + manual build Action
├── data/                        # Auto-generated by Action (committed to repo)
│   ├── meta.json                # Index metadata, source status, chunk list
│   ├── index.json               # Full merged index (all entries)
│   └── chunks/                  # Split into ~100 KB chunks for lazy loading
│       ├── chunk_000.json
│       └── ...
├── public/
│   └── index.html               # Single-file web app (the tool UI)
├── scripts/
│   └── build_index.py           # Data pipeline script
├── custom_sources.json          # Your custom data source URLs
└── README.md
```

## Data Schema (for custom JSON files)

```json
[
  {
    "id":      "my_setting_001",
    "name":    "My Custom Setting",
    "desc":    "Description of what this setting does",
    "cats":    ["My Category"],
    "plat":    "windows",
    "methods": ["intune", "registry", "gpo"],
    "intune": [{
      "defId": "device_vendor_msft_...",
      "oma":   "./Device/Vendor/MSFT/...",
      "dtype": "Integer",
      "vals":  [{"v": "1", "l": "Enabled"}, {"v": "0", "l": "Disabled"}],
      "rec":   "1",
      "json":  "\"settingDefinitionId\": \"device_vendor_msft_...\""
    }],
    "gpo": {
      "path":   "Computer Configuration > ...",
      "policy": "Policy display name",
      "admx":   "filename.admx",
      "ns":     "Namespace.String"
    },
    "reg": {
      "hive": "HKLM",
      "key":  "SOFTWARE\\Policies\\...",
      "val":  "ValueName",
      "type": "REG_DWORD",
      "data": "1=Enabled; 0=Disabled",
      "note": "Any additional notes"
    }
  }
]
```

---

Built by [Helient](https://helient.com) · Not affiliated with Microsoft
