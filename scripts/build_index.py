#!/usr/bin/env python3
"""
Helient Settings Explorer — Data Pipeline (Fixed)
==================================================
Sources:
  1. Microsoft Graph — groupPolicyDefinitions (app-only compatible)
  2. Microsoft Graph — configurationSettings (tries delegated-style query)  
  3. Snodecoder public dataset — large pre-built Intune catalog JSON
  4. Chromium policy_templates — tries multiple known URL paths
  5. Windows ADMX fallback — curated 50 key policies
  6. Office ADMX fallback — curated 25 key policies
  7. Custom sources — from custom_sources.json
"""

import json, os, sys, time, re, hashlib, argparse, urllib.request, urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.parent
DATA_DIR   = REPO_ROOT / "data"
CHUNKS_DIR = DATA_DIR / "chunks"
DATA_DIR.mkdir(exist_ok=True)
CHUNKS_DIR.mkdir(exist_ok=True)

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def http_get(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (compatible; HelientBot/1.0)"
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  HTTP error {url[:80]}: {e}")
        return None

def http_get_json(url, headers=None, timeout=45):
    raw = http_get(url, headers, timeout)
    if not raw: return None
    try: return json.loads(raw)
    except: return None

def slugify(s):
    return re.sub(r"[^a-z0-9_]", "_", s.lower())[:80]

def make_entry(source_id, entry_id, name, desc, cats, plat, methods,
               intune=None, gpo=None, admx=None, reg=None, extra=None):
    return {
        "id": entry_id, "name": name,
        "desc": (desc or "").strip()[:600],
        "cats": cats or [], "plat": plat or "windows",
        "methods": methods or [], "_source": source_id,
        "intune": intune or [], "gpo": gpo, "admx": admx, "reg": reg,
        **(extra or {}),
    }

# ── TOKEN ─────────────────────────────────────────────────────────────────────
def get_graph_token(client_id, client_secret, tenant_id):
    url  = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("access_token")
    except Exception as e:
        log(f"  Token error: {e}")
        return None

# ── OMA-URI builder ───────────────────────────────────────────────────────────
def defid_to_oma(defid):
    if not defid: return ""
    is_user = defid.lower().startswith("user_")
    prefix  = "./User" if is_user else "./Device"
    path = re.sub(r"^(device|user)_vendor_msft_", "", defid, flags=re.I)
    parts = [p[0].upper() + p[1:] for p in path.split("_") if p]
    return prefix + "/Vendor/MSFT/" + "/".join(parts)

# ── SOURCE 1: Graph groupPolicyDefinitions (app-only compatible) ──────────────
def fetch_graph_gpo(token):
    log("Fetching Graph groupPolicyDefinitions (GPO catalog)…")
    headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
    entries, seen = [], set()
    url = ("https://graph.microsoft.com/beta/deviceManagement/groupPolicyDefinitions"
           "?$select=id,classType,displayName,explainText,categoryPath,supportedOn"
           "&$top=1000")
    page = 0
    while url:
        page += 1
        log(f"  GPO page {page} — {len(entries)} so far…")
        data = http_get_json(url, headers=headers, timeout=60)
        if not data: break
        for d in data.get("value", []):
            did = d.get("id", "")
            if not did or did in seen: continue
            seen.add(did)
            name = d.get("displayName", "")
            desc = d.get("explainText", "")
            cat  = d.get("categoryPath", "Group Policy")
            plat = "windows"
            ctype = d.get("classType", "machine")
            gpo_path = (
                ("Computer" if ctype == "machine" else "User") +
                " Configuration > Administrative Templates > " + cat
            )
            e = make_entry(
                source_id="graph",
                entry_id="gpo_" + did,
                name=name,
                desc=desc,
                cats=[cat, "Group Policy Definitions"],
                plat=plat,
                methods=["gpo", "admx"],
                gpo={"path": gpo_path, "policy": name, "admx": "", "ns": ""},
            )
            entries.append(e)
        url = data.get("@odata.nextLink")
        if url: time.sleep(0.15)
    log(f"  groupPolicyDefinitions: {len(entries)} entries")
    return entries

# ── SOURCE 2: Graph configurationSettings ─────────────────────────────────────
def fetch_graph_catalog(token):
    log("Fetching Graph configurationSettings (Intune Settings Catalog)…")
    headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
    entries, seen = [], set()

    # Try multiple platform filters — this dramatically improves app-only results
    platforms = ["windows10", "macOS", "iOS", "android"]
    for plat_filter in platforms:
        url = (
            "https://graph.microsoft.com/beta/deviceManagement/configurationSettings"
            f"?$filter=applicability/platform eq '{plat_filter}'"
            "&$select=id,name,description,settingDefinitionId,applicability,"
            "defaultValue,categoryId,categoryName,options,valueDefinition,keywords"
            "&$top=1000"
        )
        page = 0
        while url:
            page += 1
            if page == 1: log(f"  Catalog platform: {plat_filter}…")
            data = http_get_json(url, headers=headers, timeout=60)
            if not data: break
            for s in data.get("value", []):
                sid = s.get("settingDefinitionId") or s.get("id", "")
                if not sid or sid in seen: continue
                seen.add(sid)
                plat = plat_filter.lower().replace("windows10", "windows").replace("macos", "macos")
                name = s.get("name") or sid
                desc = s.get("description", "")
                oma  = defid_to_oma(sid)
                vals = []
                for o in (s.get("options") or []):
                    v = str(o.get("itemId") or o.get("value") or "")
                    l = o.get("displayName") or o.get("name") or v
                    if v: vals.append({"v": v, "l": l})
                vd    = (s.get("valueDefinition") or {}).get("@odata.type", "")
                dtype = ("Choice" if vals else
                         "Integer" if "Integer" in vd else
                         "Boolean" if "Boolean" in vd else "String")
                if dtype == "Choice" and vals:
                    json_frag = (f'"settingDefinitionId": "{sid}",\n'
                                 f'"choiceSettingValue": {{"value": "{vals[0]["v"]}", "children": []}}')
                else:
                    json_frag = f'"settingDefinitionId": "{sid}"'
                e = make_entry(
                    source_id="graph",
                    entry_id="cat_" + sid,
                    name=name, desc=desc,
                    cats=[s.get("categoryName") or "Settings Catalog"],
                    plat=plat, methods=["intune"],
                    intune=[{
                        "cat": s.get("categoryName") or "Settings Catalog",
                        "name": name, "defId": sid, "oma": oma,
                        "dtype": dtype, "vals": vals,
                        "rec": str(s.get("defaultValue", "") or (vals[0]["v"] if vals else "")),
                        "json": json_frag,
                    }],
                    extra={"_infoUrl": (s.get("infoUrls") or [""])[0] if s.get("infoUrls") else ""},
                )
                entries.append(e)
            url = data.get("@odata.nextLink")
            if url: time.sleep(0.15)
    log(f"  configurationSettings: {len(entries)} entries")
    return entries

# ── SOURCE 3: Snodecoder public Intune catalog dataset ────────────────────────
# This is a publicly maintained GitHub Pages site that mirrors the full
# Intune Settings Catalog as static JSON — perfect for app-only fallback.
SNODECODER_URLS = [
    "https://raw.githubusercontent.com/zjorz/Public-MS-Docs/main/AzureAD-Intune/Intune-Settings-Catalog.json",
    "https://raw.githubusercontent.com/MicrosoftDocs/memdocs/main/memdocs/intune/configuration/settings-catalog-settings.json",
    "https://raw.githubusercontent.com/SkipToTheEndpoint/OpenIntuneBaseline/main/ADMX/Windows/PolicyDefinitions/IntuneSettingsCatalog.json",
]

# Alternative: fetch from the well-known graph explorer cache
GRAPH_EXPLORER_URLS = [
    "https://raw.githubusercontent.com/microsoftgraph/msgraph-sdk-dotnet/main/src/Microsoft.Graph/Generated/Models/DeviceManagement/ConfigurationSettings.json",
]

def fetch_snodecoder():
    """Try public Intune catalog mirrors."""
    log("Trying public Intune Settings Catalog mirrors…")
    for url in SNODECODER_URLS:
        log(f"  Trying: {url[:70]}…")
        data = http_get_json(url, timeout=60)
        if data and isinstance(data, list) and len(data) > 100:
            log(f"  Found {len(data)} entries from mirror")
            entries = []
            for item in data:
                if not isinstance(item, dict): continue
                sid = item.get("settingDefinitionId") or item.get("id", "")
                if not sid: continue
                name = item.get("name") or item.get("displayName") or sid
                desc = item.get("description") or item.get("helpText") or ""
                oma  = defid_to_oma(sid)
                e = make_entry(
                    source_id="graph",
                    entry_id="mirror_" + sid,
                    name=name, desc=desc,
                    cats=[item.get("categoryName") or "Settings Catalog"],
                    plat="windows", methods=["intune"],
                    intune=[{
                        "cat": item.get("categoryName") or "Settings Catalog",
                        "name": name, "defId": sid, "oma": oma,
                        "dtype": "Choice", "vals": [],
                        "rec": "", "json": f'"settingDefinitionId": "{sid}"',
                    }],
                )
                entries.append(e)
            if entries:
                return entries
        elif data and isinstance(data, dict):
            vals = data.get("value") or data.get("settings") or data.get("data") or []
            if len(vals) > 100:
                log(f"  Found {len(vals)} entries from dict mirror")
                entries = []
                for item in vals:
                    if not isinstance(item, dict): continue
                    sid = item.get("settingDefinitionId") or item.get("id", "")
                    if not sid: continue
                    name = item.get("name") or item.get("displayName") or sid
                    oma  = defid_to_oma(sid)
                    e = make_entry(
                        source_id="graph",
                        entry_id="mirror_" + sid,
                        name=name,
                        desc=item.get("description") or "",
                        cats=[item.get("categoryName") or "Settings Catalog"],
                        plat="windows", methods=["intune"],
                        intune=[{"cat": "Settings Catalog", "name": name,
                                 "defId": sid, "oma": oma, "dtype": "Choice",
                                 "vals": [], "rec": "",
                                 "json": f'"settingDefinitionId": "{sid}"'}],
                    )
                    entries.append(e)
                if entries:
                    return entries
    log("  No public mirrors returned usable data — Graph data limited to what app-only token returns")
    return []

# ── SOURCE 4: Chromium policy_templates ───────────────────────────────────────
# Try multiple known URL paths — the file moved in recent Chromium versions
CHROMIUM_URLS = [
    # New location (split into components)
    "https://raw.githubusercontent.com/chromium/chromium/main/components/policy/resources/templates/policy_templates.json",
    # Old location (pre-2023)
    "https://raw.githubusercontent.com/chromium/chromium/main/components/policy/resources/policy_templates.json",
    # Edge policy list (Microsoft's own public repo)
    "https://raw.githubusercontent.com/MicrosoftEdge/MSEdgeExplainers/main/ManagedConfiguration/MicrosoftEdge.json",
    # Cached copy sometimes available here
    "https://raw.githubusercontent.com/nicholasgasior/gsfmt/master/testdata/policy_templates.json",
]

# Hardcoded curated browser policies as reliable fallback
BROWSER_POLICIES_FALLBACK = [
    # (name, desc, reg_key_edge, reg_key_chrome, type, example)
    ("HomepageLocation", "Configure the home page URL", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "https://intranet.contoso.com"),
    ("HomepageIsNewTabPage", "Set the new tab page as the home page", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("NewTabPageLocation", "Configure the new tab page URL", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "https://intranet.contoso.com"),
    ("DefaultSearchProviderEnabled", "Enable the default search provider", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("DefaultSearchProviderSearchURL", "Default search provider search URL", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "https://search.contoso.com?q={searchTerms}"),
    ("PasswordManagerEnabled", "Enable saving passwords to the password manager", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("AutofillCreditCardEnabled", "Enable AutoFill for credit cards", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("AutofillAddressEnabled", "Enable AutoFill for addresses", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("SyncDisabled", "Disable synchronization of data using Google/Microsoft sync services", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("BrowserSignin", "Browser sign in settings (0=Disable, 1=Enable, 2=Force)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("ForceGoogleSafeSearch", "Force Google SafeSearch", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("SafeBrowsingEnabled", "Enable Safe Browsing", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("SafeBrowsingExtendedReportingEnabled", "Enable Safe Browsing extended reporting", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("ExtensionInstallBlocklist", "Configure extension install blocklist (* = block all)", "SOFTWARE\\Policies\\Microsoft\\Edge\\ExtensionInstallBlocklist", "SOFTWARE\\Policies\\Google\\Chrome\\ExtensionInstallBlocklist", "REG_SZ", "*"),
    ("ExtensionInstallAllowlist", "Configure extension install allowlist", "SOFTWARE\\Policies\\Microsoft\\Edge\\ExtensionInstallAllowlist", "SOFTWARE\\Policies\\Google\\Chrome\\ExtensionInstallAllowlist", "REG_SZ", "extension-id"),
    ("ExtensionInstallForcelist", "Configure the list of force-installed extensions", "SOFTWARE\\Policies\\Microsoft\\Edge\\ExtensionInstallForcelist", "SOFTWARE\\Policies\\Google\\Chrome\\ExtensionInstallForcelist", "REG_SZ", "id;update_url"),
    ("PopupsAllowedForUrls", "Allow pop-up windows on specific sites", "SOFTWARE\\Policies\\Microsoft\\Edge\\PopupsAllowedForUrls", "SOFTWARE\\Policies\\Google\\Chrome\\PopupsAllowedForUrls", "REG_SZ", "https://[*.]contoso.com"),
    ("CookiesSessionOnlyForUrls", "Limit cookies from matching URLs to the current session", "SOFTWARE\\Policies\\Microsoft\\Edge\\CookiesSessionOnlyForUrls", "SOFTWARE\\Policies\\Google\\Chrome\\CookiesSessionOnlyForUrls", "REG_SZ", "https://[*.]contoso.com"),
    ("DefaultCookiesSetting", "Default cookies setting (1=Allow, 2=Block, 4=Session only)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("DefaultJavaScriptSetting", "Default JavaScript setting (1=Allow, 2=Block)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("DefaultPluginsSetting", "Control plugins usage (2=Block)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "2"),
    ("DefaultGeolocationSetting", "Default geolocation setting (1=Allow, 2=Block, 3=Ask)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "2"),
    ("DefaultNotificationsSetting", "Default notification setting (1=Allow, 2=Block, 3=Ask)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "2"),
    ("SmartScreenEnabled", "Configure Microsoft Defender SmartScreen (Edge only)", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_DWORD", "1"),
    ("PreventSmartScreenPromptOverride", "Prevent users from bypassing SmartScreen warnings", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("SSLVersionMin", "Minimum TLS version (tls1.2 recommended)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "tls1.2"),
    ("URLBlocklist", "Block access to a list of URLs", "SOFTWARE\\Policies\\Microsoft\\Edge\\URLBlocklist", "SOFTWARE\\Policies\\Google\\Chrome\\URLBlocklist", "REG_SZ", "javascript://*"),
    ("URLAllowlist", "Allow access to a list of URLs (overrides blocklist)", "SOFTWARE\\Policies\\Microsoft\\Edge\\URLAllowlist", "SOFTWARE\\Policies\\Google\\Chrome\\URLAllowlist", "REG_SZ", "https://[*.]contoso.com"),
    ("IncognitoModeAvailability", "Incognito mode availability (0=Allow, 1=Disable, 2=Force)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("PrintingEnabled", "Enable printing", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("DownloadRestrictions", "Allow or block downloads (0=No restriction, 3=Block all)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("SaverModeEnabled", "Enable Edge's budget saver mode", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_DWORD", "1"),
    ("EdgeShoppingAssistantEnabled", "Enable shopping in Microsoft Edge", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_DWORD", "0"),
    ("PersonalizationReportingEnabled", "Allow personalization of ads and browser by sending browsing history", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_DWORD", "0"),
    ("HideFirstRunExperience", "Hide the First-run experience and splash screen", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("BackgroundModeEnabled", "Continue running background apps after browser is closed", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("UpdatesEnabled", "Allow Microsoft Edge to be updated (false=Disable auto-update)", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_DWORD", "1"),
    ("TargetChannel", "Target release channel (stable, beta, dev)", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_SZ", "stable"),
    ("AutoUpdateCheckPeriodMinutes", "Override the minimum auto-update check period", None, "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "43200"),
    ("CloudManagementEnrollmentToken", "Set the cloud policy enrollment token", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "<token>"),
    ("EnterpriseModeSiteListFileUrl", "Configure the Enterprise Mode Site List (Edge/IE compat)", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_SZ", "https://sitelist.contoso.com/sitelist.xml"),
    ("InternetExplorerIntegrationLevel", "Configure Internet Explorer integration mode", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_DWORD", "1"),
    ("ManagedFavorites", "Configure favorites in Edge", "SOFTWARE\\Policies\\Microsoft\\Edge", None, "REG_SZ", "[{\"toplevel_name\":\"Contoso\"}]"),
    ("ManagedBookmarks", "Managed bookmarks for Chrome", None, "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "[{\"toplevel_name\":\"Contoso\"}]"),
    ("ShowHomeButton", "Show Home button on toolbar", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("BookmarkBarEnabled", "Enable bookmark bar", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "1"),
    ("DeveloperToolsAvailability", "Control where developer tools can be used (0=Allow, 2=Disallow)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "2"),
    ("SpellCheckServiceEnabled", "Enable or disable spell check web service", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("MetricsReportingEnabled", "Enable usage and crash-related data reporting", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("SearchSuggestEnabled", "Enable search suggestions", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("TranslateEnabled", "Enable Translate", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "0"),
    ("NetworkPredictionOptions", "Enable network prediction (0=Always, 2=Never)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_DWORD", "2"),
    ("WebRtcIPHandling", "WebRTC IP handling policy", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "disable_non_proxied_udp"),
    ("CertificateTransparencyEnforcementDisabledForUrls", "Disable Certificate Transparency enforcement for specific URLs", "SOFTWARE\\Policies\\Microsoft\\Edge\\CertificateTransparencyEnforcementDisabledForUrls", "SOFTWARE\\Policies\\Google\\Chrome\\CertificateTransparencyEnforcementDisabledForUrls", "REG_SZ", "example.com"),
    ("AuthSchemes", "Supported authentication schemes (basic,digest,ntlm,negotiate)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "ntlm,negotiate"),
    ("AuthServerAllowlist", "Authentication server allowlist", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "*.contoso.com,contoso.com"),
    ("ProxyMode", "Configure proxy settings mode (direct, auto_detect, pac_script, fixed_servers)", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "direct"),
    ("ProxyServer", "Address or URL of proxy server", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "proxy.contoso.com:8080"),
    ("ProxyPacUrl", "URL to a proxy .pac file", "SOFTWARE\\Policies\\Microsoft\\Edge", "SOFTWARE\\Policies\\Google\\Chrome", "REG_SZ", "https://proxy.contoso.com/proxy.pac"),
]

def build_browser_fallback():
    entries = []
    for (name, desc, reg_edge, reg_chrome, dtype, example) in BROWSER_POLICIES_FALLBACK:
        browsers = []
        if reg_edge:   browsers.append("Microsoft Edge")
        if reg_chrome: browsers.append("Google Chrome")
        browsers_str = " / ".join(browsers)
        note_parts = []
        if reg_edge:   note_parts.append(f"Edge: HKLM\\{reg_edge}\\{name}")
        if reg_chrome: note_parts.append(f"Chrome: HKLM\\{reg_chrome}\\{name}")
        reg = None
        if reg_edge:
            reg = {
                "hive": "HKLM",
                "key":  reg_edge,
                "val":  name,
                "type": dtype,
                "data": example,
                "note": "  |  ".join(note_parts),
            }
        e = make_entry(
            source_id="chromium",
            entry_id="browser_" + slugify(name),
            name=f"{name} ({browsers_str})",
            desc=desc,
            cats=["Browser Policy"] + browsers,
            plat="windows",
            methods=["gpo", "registry", "admx"],
            gpo={
                "path":   f"Computer Configuration > Administrative Templates > {browsers_str}",
                "policy": name,
                "admx":   "MSEdge.admx  /  chrome.admx",
                "ns":     "Microsoft.Policies.Edge  /  Google.Policies.Chrome",
            },
            admx={
                "name":   name,
                "file":   "MSEdge.admx or chrome.admx",
                "cat":    "Browser Policy",
                "regKey": (f"HKLM\\{reg_edge}" if reg_edge else f"HKLM\\{reg_chrome}"),
                "val":    name,
                "type":   dtype,
            },
            reg=reg,
        )
        entries.append(e)
    return entries

def fetch_chromium():
    log("Fetching Chromium policy_templates.json…")
    for url in CHROMIUM_URLS:
        log(f"  Trying {url[:70]}…")
        raw = http_get(url, timeout=60)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except:
            continue
        policies = (data.get("policy_definitions") or
                    data.get("policies") or
                    data.get("policy_templates") or
                    (data if isinstance(data, list) else None))
        if not policies or len(policies) < 50:
            continue
        log(f"  Got {len(policies)} policies from {url[:60]}")
        entries = []
        for p in policies:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            name  = p["name"]
            desc  = re.sub(r"<[^>]+>", "", p.get("desc") or p.get("caption") or "").strip()
            ptype = p.get("type", "string")
            rtype = "REG_DWORD" if ptype in ("boolean","integer","main") else "REG_SZ"
            ex    = p.get("example_value")
            ex_s  = json.dumps(ex) if ex is not None else ""
            apps  = p.get("supported_on") or []
            browsers = []
            if any("chrome" in str(a).lower() for a in apps): browsers.append("Google Chrome")
            if any("edge"   in str(a).lower() for a in apps): browsers.append("Microsoft Edge")
            if not browsers: browsers = ["Google Chrome", "Microsoft Edge"]
            bs = " / ".join(browsers)
            e = make_entry(
                source_id="chromium",
                entry_id="chrome_" + name,
                name=f"{name} ({bs} Policy)",
                desc=desc or f"Browser policy: {name}",
                cats=["Browser Policy"] + browsers,
                plat="windows",
                methods=["gpo", "registry", "admx"],
                gpo={
                    "path":   f"Computer Configuration > Administrative Templates > {bs}",
                    "policy": name,
                    "admx":   "MSEdge.admx  /  chrome.admx",
                    "ns":     "Microsoft.Policies.Edge  /  Google.Policies.Chrome",
                },
                admx={"name": name, "file": "MSEdge.admx or chrome.admx",
                      "cat": (p.get("categories") or [""])[0],
                      "regKey": "HKLM\\SOFTWARE\\Policies\\Microsoft\\Edge",
                      "val": name, "type": rtype},
                reg={"hive": "HKLM",
                     "key":  "SOFTWARE\\Policies\\Microsoft\\Edge",
                     "val":  name, "type": rtype, "data": ex_s,
                     "note": f"Chrome: HKLM\\SOFTWARE\\Policies\\Google\\Chrome\\{name}"},
            )
            entries.append(e)
        if entries:
            log(f"  Chromium: {len(entries)} policies parsed")
            return entries
    log("  All Chromium URLs failed — using curated browser policy fallback")
    entries = build_browser_fallback()
    log(f"  Browser fallback: {len(entries)} policies")
    return entries

# ── SOURCE 5: Windows ADMX ────────────────────────────────────────────────────
WINDOWS_ADMX = [
    ("Turn off Microsoft Defender Antivirus","Windows Components > Microsoft Defender Antivirus","Turn off Microsoft Defender Antivirus","WindowsDefender.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender","DisableAntiSpyware","REG_DWORD","Disables the Defender AV engine entirely. Set 0 to enable (default). Do not set unless using a third-party AV."),
    ("Configure SmartScreen for Explorer","Windows Components > Windows Defender SmartScreen > Explorer","Configure Windows Defender SmartScreen","WindowsExplorer.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\System","EnableSmartScreen","REG_DWORD","Enables SmartScreen for files downloaded via Windows Explorer. 1=Warn, 2=Block."),
    ("BitLocker — Require additional startup auth","Windows Components > BitLocker Drive Encryption > Operating System Drives","Require additional authentication at startup","VolumeEncryption.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE","UseAdvancedStartup","REG_DWORD","Requires TPM+PIN or startup key at boot. Must be 1 to require PIN."),
    ("BitLocker — OS Drive Encryption Method","Windows Components > BitLocker Drive Encryption > Operating System Drives","Choose the drive encryption method and cipher strength (Windows 10+)","VolumeEncryption.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE","EncryptionMethodWithXtsOs","REG_DWORD","6=XTS-AES 128-bit, 7=XTS-AES 256-bit (recommended)."),
    ("BitLocker — Fixed Drive Encryption Method","Windows Components > BitLocker Drive Encryption > Fixed Data Drives","Choose drive encryption method and cipher strength","VolumeEncryption.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE","EncryptionMethodWithXtsFdv","REG_DWORD","6=XTS-AES 128-bit, 7=XTS-AES 256-bit (recommended)."),
    ("BitLocker — Deny write to fixed drives not protected","Windows Components > BitLocker Drive Encryption > Fixed Data Drives","Deny write access to fixed drives not protected by BitLocker","VolumeEncryption.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE","FDVDenyWriteAccess","REG_DWORD","1=Deny write to unencrypted fixed drives."),
    ("BitLocker — Deny write to removable drives not protected","Windows Components > BitLocker Drive Encryption > Removable Data Drives","Deny write access to removable drives not protected by BitLocker","VolumeEncryption.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE","RDVDenyWriteAccess","REG_DWORD","1=Deny write to unencrypted USB/removable drives."),
    ("Turn On Virtualization Based Security","System > Device Guard","Turn On Virtualization Based Security","DeviceGuard.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DeviceGuard","EnableVirtualizationBasedSecurity","REG_DWORD","Enables VBS required for Credential Guard and HVCI. 1=Enabled."),
    ("HVCI — Virtualization Based Protection of Code Integrity","System > Device Guard","Virtualization Based Protection of Code Integrity","DeviceGuard.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DeviceGuard","HypervisorEnforcedCodeIntegrity","REG_DWORD","1=Enabled without UEFI lock, 2=Enabled with UEFI lock (cannot be disabled without re-imaging)."),
    ("Credential Guard","System > Device Guard","Turn On Virtualization Based Security — Credential Guard Configuration","DeviceGuard.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DeviceGuard","LsaCfgFlags","REG_DWORD","1=Enabled without UEFI lock, 2=Enabled with UEFI lock."),
    ("LSA Protected Process","Windows Settings > Security Settings > Local Policies > Security Options","LSASS running as a protected process","SecGuide.admx","HKLM\\SYSTEM\\CurrentControlSet\\Control\\Lsa","RunAsPPL","REG_DWORD","1=Enabled (PPL), 2=Enabled (PPL lite). Prevents credential dumping from LSASS."),
    ("LAPS — Configure password backup directory","System > LAPS","Configure password backup directory","LAPS.admx","HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\LAPS","BackupDirectory","REG_DWORD","0=Disabled, 1=Back up to AAD, 2=Back up to Active Directory."),
    ("LAPS — Password age policy","System > LAPS","Password Settings","LAPS.admx","HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\LAPS","PasswordAgeDays","REG_DWORD","Maximum age in days before LAPS rotates the local admin password."),
    ("Windows Update — Specify deadline for quality updates","Windows Components > Windows Update > Windows Update for Business","Specify deadline before auto-restart for quality update","WindowsUpdate.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate","ConfigureDeadlineForQualityUpdates","REG_DWORD","Number of days (0–30) before quality updates are force-installed."),
    ("Windows Update — Specify deadline for feature updates","Windows Components > Windows Update > Windows Update for Business","Specify deadline before auto-restart for feature update","WindowsUpdate.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate","ConfigureDeadlineForFeatureUpdates","REG_DWORD","Number of days (0–30) before feature updates are force-installed."),
    ("Windows Update — Defer quality updates","Windows Components > Windows Update > Windows Update for Business","Select when Quality Updates are received","WindowsUpdate.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate","DeferQualityUpdatesPeriodInDays","REG_DWORD","Number of days (0–35) to defer quality updates."),
    ("Windows Update — Defer feature updates","Windows Components > Windows Update > Windows Update for Business","Select when Preview Builds and Feature Updates are received","WindowsUpdate.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate","DeferFeatureUpdatesPeriodInDays","REG_DWORD","Number of days (0–365) to defer feature updates."),
    ("Windows Update — Configure Active Hours","Windows Components > Windows Update","Turn off auto-restart for updates during active hours","WindowsUpdate.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU","ActiveHoursStart","REG_DWORD","Hour (0–23) when active hours start. Auto-restart won't occur during this window."),
    ("UAC — Admin Approval Mode","Windows Settings > Security Settings > Local Policies > Security Options","User Account Control: Run all administrators in Admin Approval Mode","MSS-legacy.admx","HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System","EnableLUA","REG_DWORD","1=Enabled. Disabling this completely disables UAC — not recommended."),
    ("UAC — Elevation prompt behavior for admins","Windows Settings > Security Settings > Local Policies > Security Options","User Account Control: Behavior of the elevation prompt for administrators in Admin Approval Mode","MSS-legacy.admx","HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System","ConsentPromptBehaviorAdmin","REG_DWORD","0=No prompt, 1=Credentials (secure desktop), 2=Consent (secure desktop), 5=Consent (default)."),
    ("UAC — Elevation prompt behavior for standard users","Windows Settings > Security Settings > Local Policies > Security Options","User Account Control: Behavior of the elevation prompt for standard users","MSS-legacy.admx","HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System","ConsentPromptBehaviorUser","REG_DWORD","0=Auto-deny, 1=Credentials (secure desktop), 3=Credentials (default)."),
    ("Windows Defender Firewall — Domain Profile","Windows Settings > Security Settings > Windows Defender Firewall","Windows Defender Firewall: Domain Profile — Firewall State","WindowsFirewall.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\DomainProfile","EnableFirewall","REG_DWORD","1=Enable firewall for domain-joined networks."),
    ("Windows Defender Firewall — Private Profile","Windows Settings > Security Settings > Windows Defender Firewall","Windows Defender Firewall: Standard Profile — Firewall State","WindowsFirewall.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\StandardProfile","EnableFirewall","REG_DWORD","1=Enable firewall for private (trusted) networks."),
    ("Windows Defender Firewall — Public Profile","Windows Settings > Security Settings > Windows Defender Firewall","Windows Defender Firewall: Public Profile — Firewall State","WindowsFirewall.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\PublicProfile","EnableFirewall","REG_DWORD","1=Enable firewall for public (untrusted) networks."),
    ("Windows Defender Firewall — Block inbound by default (Domain)","Windows Settings > Security Settings > Windows Defender Firewall","Windows Defender Firewall: Domain — Inbound connections","WindowsFirewall.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\DomainProfile","DefaultInboundAction","REG_DWORD","1=Block inbound connections not matching a rule (recommended)."),
    ("ASR — Configure Attack Surface Reduction rules","Windows Components > Microsoft Defender Antivirus > Microsoft Defender Exploit Guard > Attack Surface Reduction","Configure Attack Surface Reduction rules","WindowsDefender.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Windows Defender Exploit Guard\\ASR\\Rules","<rule-GUID>","REG_SZ","Map ASR rule GUIDs to 0=Disable, 1=Block, 2=Audit. Key rule: d4f940ab-401b-4efc-aadc-ad5f3c50688a (Office spawning child processes)."),
    ("Defender — Cloud-delivered protection level","Windows Components > Microsoft Defender Antivirus > MpEngine","Select cloud protection level","WindowsDefender.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\MpEngine","MpCloudBlockLevel","REG_DWORD","0=Default, 2=High, 4=High+, 6=Zero tolerance. Recommended: 2."),
    ("Defender — PUA Protection","Windows Components > Microsoft Defender Antivirus","Configure detection for potentially unwanted applications","WindowsDefender.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender","PUAProtection","REG_DWORD","1=Block PUA, 2=Audit mode, 0=Disabled."),
    ("Defender — Real-time Protection","Windows Components > Microsoft Defender Antivirus > Real-time Protection","Turn off real-time protection","WindowsDefender.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Real-Time Protection","DisableRealtimeMonitoring","REG_DWORD","0=Real-time protection ON (recommended). 1=Disabled."),
    ("Windows Hello for Business","Windows Components > Windows Hello for Business","Use Windows Hello for Business","Passport.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\PassportForWork","Enabled","REG_DWORD","1=Enable WHfB as primary auth method replacing passwords."),
    ("Windows Hello — Require PIN complexity","Windows Components > Windows Hello for Business > PIN Complexity","Require digits","Passport.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\PassportForWork\\PINComplexity","Digits","REG_DWORD","1=Require digits in WHfB PIN."),
    ("Remote Desktop — Require NLA","Windows Components > Remote Desktop Services > Remote Desktop Session Host > Security","Require user authentication for remote connections by using NLA","TerminalServer.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows NT\\Terminal Services","UserAuthentication","REG_DWORD","1=Require Network Level Authentication before RDP session is established."),
    ("Remote Desktop — Allow connections","Windows Components > Remote Desktop Services > Remote Desktop Session Host > Connections","Allow users to connect remotely by using Remote Desktop Services","TerminalServer.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows NT\\Terminal Services","fDenyTSConnections","REG_DWORD","0=Allow RDP connections. 1=Block (default on client SKUs)."),
    ("Remote Desktop — Set encryption level","Windows Components > Remote Desktop Services > Remote Desktop Session Host > Security","Set client connection encryption level","TerminalServer.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows NT\\Terminal Services","MinEncryptionLevel","REG_DWORD","3=High (128-bit) encryption. Recommended."),
    ("OneDrive — Silently sign in","OneDrive","Silently sign in users to the OneDrive sync app with their Windows credentials","OneDrive.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\OneDrive","SilentAccountConfig","REG_DWORD","1=Silently sign users into OneDrive using their AAD credentials."),
    ("OneDrive — Known Folder Move silent opt-in","OneDrive","Silently move Windows known folders to OneDrive","OneDrive.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\OneDrive","KFMSilentOptIn","REG_SZ","Tenant ID. Silently redirects Desktop, Documents, Pictures to OneDrive."),
    ("OneDrive — Prevent non-corporate accounts","OneDrive","Prevent users from adding personal OneDrive accounts","OneDrive.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\OneDrive","DisablePersonalSync","REG_DWORD","1=Block personal Microsoft account OneDrive sync."),
    ("OneDrive — Block file sync for specific apps","OneDrive","Block specific apps from syncing with OneDrive","OneDrive.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\OneDrive\\BlockExternalSync","<AppName>","REG_SZ","Prevents specified apps from syncing files via OneDrive."),
    ("Machine inactivity lockout","Windows Settings > Security Settings > Local Policies > Security Options","Interactive logon: Machine inactivity limit","SecGuide.admx","HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System","InactivityTimeoutSecs","REG_DWORD","Seconds of inactivity before workstation locks. 900=15 minutes."),
    ("Screen saver timeout","Control Panel > Personalization","Screen saver timeout","conf.admx","HKCU\\Software\\Policies\\Microsoft\\Windows\\Control Panel\\Desktop","ScreenSaveTimeOut","REG_SZ","Idle seconds before screen saver starts. 900=15 minutes."),
    ("Audit logon events","Windows Settings > Security Settings > Advanced Audit Policy","Audit Logon","auditpol","N/A — configured via secpol.msc or auditpol.exe","","","Configures auditing of logon/logoff events. Both success and failure recommended."),
    ("Power — Require password on wake (AC)","System > Power Management > Sleep Settings","Require a password when a computer wakes (plugged in)","Power.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Power\\PowerSettings\\0e796bdb-100d-47d6-a2d5-f7d2daa51f51","ACSettingIndex","REG_DWORD","1=Require password on wake from sleep (AC power)."),
    ("Power — Require password on wake (DC)","System > Power Management > Sleep Settings","Require a password when a computer wakes (on battery)","Power.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Power\\PowerSettings\\0e796bdb-100d-47d6-a2d5-f7d2daa51f51","DCSettingIndex","REG_DWORD","1=Require password on wake from sleep (battery power)."),
    ("Disable autorun for all drives","Windows Components > AutoPlay Policies","Disallow Autorun for non-volume devices","AutoPlay.admx","HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer","NoDriveTypeAutoRun","REG_DWORD","255=Disable AutoRun for all drive types including USB."),
    ("Windows Installer — Prevent elevated installs","Windows Components > Windows Installer","Prevent users from installing software","MSI.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\Installer","DisableMSI","REG_DWORD","1=Prevent users from installing MSI packages outside of software deployment."),
    ("Windows Installer — Always install with elevated privileges","Windows Components > Windows Installer","Always install with elevated privileges","MSI.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\Installer","AlwaysInstallElevated","REG_DWORD","0=Disable (recommended). 1=Allows users to install with elevated rights — security risk."),
    ("Disable Guest account","Windows Settings > Security Settings > Local Policies > Security Options","Accounts: Guest account status","SecGuide.admx","HKLM\\SAM\\SAM\\Domains\\Account\\Users\\000001F5","F","REG_BINARY","Disable the built-in Guest account. Configured via secpol.msc > Local Policies > Security Options."),
    ("Rename Administrator account","Windows Settings > Security Settings > Local Policies > Security Options","Accounts: Rename administrator account","SecGuide.admx","N/A — configured via secpol.msc","","","Renames the built-in Administrator account to reduce attack surface."),
    ("SMBv1 — Disable","N/A — Registry only (not an ADMX policy)","No ADMX policy — configure via registry or PowerShell","N/A","HKLM\\SYSTEM\\CurrentControlSet\\Services\\LanmanServer\\Parameters","SMB1","REG_DWORD","0=Disable SMBv1 (required for EternalBlue/WannaCry protection). Restart required."),
    ("WDigest Authentication — Disable","Windows Settings > Security Settings > Local Policies > Security Options","WDigest Authentication","SecGuide.admx","HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecurityProviders\\WDigest","UseLogonCredential","REG_DWORD","0=Disable WDigest (prevents plaintext credentials in memory). Recommended."),
    ("Telemetry — Diagnostic data level","Windows Components > Data Collection and Preview Builds","Allow Diagnostic Data","DataCollection.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection","AllowTelemetry","REG_DWORD","0=Security (Enterprise only), 1=Required, 3=Optional. Enterprise: set to 0 or 1."),
    ("Restrict removable storage — Read","System > Removable Storage Access","All Removable Storage Classes: Deny read access","RemovableStorage.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\RemovableStorageDevices","Deny_Read","REG_DWORD","1=Deny all read access to removable storage devices."),
    ("Restrict removable storage — Write","System > Removable Storage Access","All Removable Storage Classes: Deny write access","RemovableStorage.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\RemovableStorageDevices","Deny_Write","REG_DWORD","1=Deny all write access to removable storage devices."),
]

def fetch_windows_admx():
    log("Building Windows ADMX entries…")
    entries = []
    for row in WINDOWS_ADMX:
        name, gpo_path, policy, admx_file, reg_key, val, dtype, desc = row
        hive = "HKCU" if reg_key.startswith("HKCU") else "HKLM"
        key  = reg_key.replace("HKLM\\","").replace("HKCU\\","")
        full_gpo_path = "Computer Configuration > Administrative Templates > " + gpo_path
        e = make_entry(
            source_id="admx_windows",
            entry_id="admx_win_" + slugify(name),
            name=name, desc=desc,
            cats=["Administrative Templates", "Windows Policy",
                  gpo_path.split(" > ")[0] if " > " in gpo_path else gpo_path],
            plat="windows", methods=["gpo","admx","registry"],
            gpo={"path": full_gpo_path, "policy": policy, "admx": admx_file, "ns": ""},
            admx={"name": policy, "file": admx_file, "cat": gpo_path,
                  "regKey": reg_key, "val": val, "type": dtype},
            reg={"hive": hive, "key": key, "val": val, "type": dtype,
                 "data": "See description", "note": ""},
        )
        entries.append(e)
    log(f"  Windows ADMX: {len(entries)} entries")
    return entries

# ── SOURCE 6: Office ADMX ─────────────────────────────────────────────────────
OFFICE_ADMX = [
    ("Block macros from Internet — All Office Apps","Blocks VBA macros in Office files downloaded from the internet. 1=Block.","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security","blockcontentexecutionfrominternet","REG_DWORD"),
    ("VBA Macro Warnings — Word","1=Enable all, 2=Disable+Notify, 3=Disable all, 4=Signed macros only.","word16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Security","VBAWarnings","REG_DWORD"),
    ("VBA Macro Warnings — Excel","1=Enable all, 2=Disable+Notify, 3=Disable all, 4=Signed macros only.","excel16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Excel\\Security","VBAWarnings","REG_DWORD"),
    ("VBA Macro Warnings — PowerPoint","1=Enable all, 2=Disable+Notify, 3=Disable all, 4=Signed macros only.","ppt16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\PowerPoint\\Security","VBAWarnings","REG_DWORD"),
    ("VBA Macro Warnings — Outlook","Controls macro execution in Outlook. 2=Disable with notification (recommended).","outlk16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Security","VBAWarnings","REG_DWORD"),
    ("Disable Trusted Documents — Word","Prevents users marking documents as trusted, enforcing security checks every open.","word16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Security\\Trusted Documents","DisableTrustedRecords","REG_DWORD"),
    ("Disable Trusted Documents — Excel","Prevents users marking workbooks as trusted.","excel16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Excel\\Security\\Trusted Documents","DisableTrustedRecords","REG_DWORD"),
    ("Disable Office Store Add-ins","Blocks web add-ins from the Office Store. 1=Block.","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security","DisableStoreApps","REG_DWORD"),
    ("Enable Protected View — Internet Files","Opens internet-sourced files in Protected View (read-only sandbox). 1=Enable.","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security","protectedviewdisableforunsafelocations","REG_DWORD"),
    ("Enable Protected View — Email Attachments","Opens email attachments in Protected View. 0=Enable (0=NOT disabled).","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security","protectedviewdisableforattachments","REG_DWORD"),
    ("Enable Protected View — Unsafe Locations","Opens files from unsafe locations in Protected View. 0=Enable.","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security","protectedviewdisableforunsafelocations","REG_DWORD"),
    ("Disable All ActiveX — Office","Disables all ActiveX controls in Office documents. 1=Disable all.","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security","disableallactivex","REG_DWORD"),
    ("Outlook — Block automatic download of external content","Prevents Outlook auto-downloading images from external sources. 1=Block.","outlk16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Options\\Mail","blockexternalcontent","REG_DWORD"),
    ("Outlook — Disable automatic forwarding","Prevents automatic email forwarding to external addresses.","outlk16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Options\\Mail","disableautoforward","REG_DWORD"),
    ("Outlook — S/MIME require signed receipt","Requires signed receipts for all S/MIME messages.","outlk16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Security","RequireSMIMEReceipt","REG_DWORD"),
    ("Outlook — Junk email protection level","0=No protection, 1=Low, 2=High, 3=Safe lists only.","outlk16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Options\\Mail","junkmaillevel","REG_DWORD"),
    ("Office Updates — Enable automatic updates (C2R)","1=Enable automatic updates for Microsoft 365 Apps.","office16.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Office\\16.0\\Common\\OfficeUpdate","EnableAutomaticUpdates","REG_DWORD"),
    ("Office Updates — Update channel","Specify update channel: Current, MonthlyEnterprise, SemiAnnual, SemiAnnualPreview.","office16.admx","HKLM\\SOFTWARE\\Policies\\Microsoft\\Office\\16.0\\Common\\OfficeUpdate","UpdateBranch","REG_SZ"),
    ("Word — Block old format files (Word 2 and earlier)","2=Block open of Word 2 and earlier format files.","word16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Security\\FileBlock","Word2AndEarlier","REG_DWORD"),
    ("Excel — Block old format XLS files","2=Block open of Excel 4 workbook format files.","excel16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Excel\\Security\\FileBlock","XL4Workbooks","REG_DWORD"),
    ("PowerPoint — Block old format PPT files","2=Block open of PowerPoint 97–2003 format files.","ppt16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\PowerPoint\\Security\\FileBlock","PowerPoint97Files","REG_DWORD"),
    ("Disable DDE in Word","0=Disable DDE (Dynamic Data Exchange) feature in Word to prevent DDE-based attacks.","word16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Options","DDEAllowed","REG_DWORD"),
    ("SharePoint — Map trusted sites","Adds SharePoint to trusted sites zone for seamless NTLM/Kerberos auth.","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Internet\\Server Cache","SharePointSiteList","REG_SZ"),
    ("Teams — Prevent auto-start on Windows logon","Prevents Microsoft Teams from starting automatically at Windows startup.","skype16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Lync","AutoRun","REG_DWORD"),
    ("Office — Disable use of personal information","Prevents Office from using personal info in document properties.","office16.admx","HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common","qmenable","REG_DWORD"),
]

def fetch_office_admx():
    log("Building Office ADMX entries…")
    entries = []
    app_map = {
        "word16":"Microsoft Word", "excel16":"Microsoft Excel",
        "ppt16":"Microsoft PowerPoint", "outlk16":"Microsoft Outlook",
        "office16":"Microsoft Office", "skype16":"Microsoft Teams",
    }
    for row in OFFICE_ADMX:
        name, desc, admx_file, reg_key, val, dtype = row
        hive = "HKCU" if reg_key.startswith("HKCU") else "HKLM"
        key  = reg_key.replace("HKLM\\","").replace("HKCU\\","")
        app  = next((v for k,v in app_map.items() if k in admx_file), "Microsoft Office")
        oma  = (f"./User/Vendor/MSFT/Policy/Config/"
                f"ADMX_{admx_file.replace('.admx','').replace('16','2016')}"
                f"~Policy~L_{app.replace(' ','_')}/{slugify(name)}")
        e = make_entry(
            source_id="admx_office",
            entry_id="admx_office_" + slugify(name),
            name=name, desc=desc,
            cats=[app, "Microsoft Office", "Security"],
            plat="windows",
            methods=["gpo","admx","registry","intune"],
            gpo={"path": f"User/Computer Configuration > Administrative Templates > {app}",
                 "policy": name, "admx": admx_file,
                 "ns": f"Microsoft.Policies.{app.replace(' ','')}"},
            admx={"name": name, "file": admx_file, "cat": app,
                  "regKey": reg_key, "val": val, "type": dtype},
            reg={"hive": hive, "key": key, "val": val, "type": dtype,
                 "data": "See description", "note": ""},
            intune=[{
                "cat": app, "name": name, "defId": "", "oma": oma,
                "dtype": "String (ADMX Ingestion)",
                "vals": [{"v":"<enabled/>","l":"Enabled"},{"v":"<disabled/>","l":"Disabled"}],
                "rec": "<enabled/>",
                "json": f'"@odata.type": "#microsoft.graph.deviceManagementConfigurationSetting"',
            }],
        )
        entries.append(e)
    log(f"  Office ADMX: {len(entries)} entries")
    return entries

# ── SOURCE 7: Custom sources ──────────────────────────────────────────────────
def fetch_custom():
    log("Loading custom sources…")
    entries = []
    custom_file = REPO_ROOT / "custom_sources.json"
    if custom_file.exists():
        try:
            sources = json.loads(custom_file.read_text()).get("sources", [])
            for src in sources:
                if src.get("_example"): continue
                url  = src.get("url","")
                name = src.get("name", url)
                if not url: continue
                log(f"  Fetching: {name}")
                raw = http_get(url, timeout=30)
                if not raw: continue
                try:
                    data = json.loads(raw)
                    items = data if isinstance(data,list) else data.get("settings",data.get("entries",[]))
                    for item in items:
                        item.setdefault("_source","custom")
                        if not item.get("id"):
                            item["id"] = "custom_" + hashlib.md5(str(item).encode()).hexdigest()[:10]
                    entries.extend(items)
                    log(f"  Loaded {len(items)} from {name}")
                except Exception as e:
                    log(f"  Error loading {name}: {e}")
        except Exception as e:
            log(f"  Error reading custom_sources.json: {e}")
    log(f"  Custom: {len(entries)} entries")
    return entries

# ── SEARCH TEXT ───────────────────────────────────────────────────────────────
def build_search_text(e):
    g = e.get("gpo") or {}
    a = e.get("admx") or {}
    r = e.get("reg") or {}
    parts = [
        e.get("name",""), e.get("desc",""),
        " ".join(e.get("cats",[])), e.get("plat",""),
        " ".join(e.get("methods",[])),
        g.get("path",""), g.get("policy",""),
        a.get("name",""), a.get("regKey",""), a.get("file",""),
        r.get("key",""), r.get("val",""),
    ]
    for i in (e.get("intune") or []):
        parts += [i.get("defId",""), i.get("name",""), i.get("oma","")]
    return " ".join(p for p in parts if p).lower()

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default="all")
    args    = parser.parse_args()
    filters = args.sources.lower().split(",")

    log("=" * 60)
    log("Helient Settings Explorer — Index Builder (Fixed)")
    log("=" * 60)

    all_entries    = []
    source_status  = {}

    def want(name): return "all" in filters or name in filters

    # Graph
    if want("graph"):
        cid  = os.environ.get("AZURE_CLIENT_ID","")
        csec = os.environ.get("AZURE_CLIENT_SECRET","")
        tid  = os.environ.get("AZURE_TENANT_ID","common")
        if cid and csec:
            token = get_graph_token(cid, csec, tid)
            if token:
                gpo_entries = fetch_graph_gpo(token)
                all_entries.extend(gpo_entries)
                cat_entries = fetch_graph_catalog(token)
                all_entries.extend(cat_entries)
                total = len(gpo_entries) + len(cat_entries)
                source_status["graph"] = {"ok": True, "count": total}
            else:
                log("  Graph auth failed")
                source_status["graph"] = {"ok": False, "count": 0, "error": "Auth failed"}
        else:
            log("  No Graph credentials — skipping live Graph fetch")
            source_status["graph"] = {"ok": False, "count": 0, "error": "No credentials"}

        # Always try the public mirror regardless of credentials
        mirror = fetch_snodecoder()
        if mirror:
            all_entries.extend(mirror)
            existing = source_status.get("graph", {})
            source_status["graph"] = {
                "ok": True,
                "count": existing.get("count", 0) + len(mirror)
            }

    if want("chromium"):
        entries = fetch_chromium()
        all_entries.extend(entries)
        source_status["chromium"] = {"ok": len(entries) > 0, "count": len(entries)}

    if want("admx"):
        entries = fetch_windows_admx()
        all_entries.extend(entries)
        source_status["admx_windows"] = {"ok": True, "count": len(entries)}

    if want("office"):
        entries = fetch_office_admx()
        all_entries.extend(entries)
        source_status["admx_office"] = {"ok": True, "count": len(entries)}

    if want("custom"):
        entries = fetch_custom()
        all_entries.extend(entries)
        source_status["custom"] = {"ok": True, "count": len(entries)}

    # Dedup by id
    seen, deduped = set(), []
    for e in all_entries:
        eid = e.get("id","")
        if eid and eid not in seen:
            seen.add(eid)
            deduped.append(e)
        elif not eid:
            deduped.append(e)

    log(f"\nTotal entries after dedup: {len(deduped)}")

    # Search text
    for e in deduped:
        e["_text"] = build_search_text(e)

    # Write full index
    idx_path = DATA_DIR / "index.json"
    idx_path.write_text(json.dumps(deduped, separators=(",",":"), ensure_ascii=False))
    idx_size = idx_path.stat().st_size
    log(f"Wrote index.json: {idx_size//1024} KB")

    # Write chunks
    CHUNK_SIZE = 200
    for old in CHUNKS_DIR.glob("chunk_*.json"): old.unlink()
    chunk_meta = []
    for i, start in enumerate(range(0, len(deduped), CHUNK_SIZE)):
        chunk = deduped[start:start+CHUNK_SIZE]
        cp    = CHUNKS_DIR / f"chunk_{i:03d}.json"
        cp.write_text(json.dumps(chunk, separators=(",",":"), ensure_ascii=False))
        chunk_meta.append({"file": f"data/chunks/chunk_{i:03d}.json",
                           "start": start, "count": len(chunk)})
    log(f"Wrote {len(chunk_meta)} chunks")

    meta = {
        "last_updated":  datetime.now(timezone.utc).isoformat(),
        "total_entries": len(deduped),
        "chunk_count":   len(chunk_meta),
        "chunks":        chunk_meta,
        "sources":       source_status,
        "index_size_kb": idx_size // 1024,
    }
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    log("Wrote meta.json")

    log("\n" + "=" * 60)
    log("Build complete!")
    log(f"  Total: {len(deduped)} settings")
    for src, info in source_status.items():
        s = "✓" if info["ok"] else "✗"
        log(f"  {s} {src}: {info['count']}")
    log("=" * 60)

if __name__ == "__main__":
    main()
