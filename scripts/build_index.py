#!/usr/bin/env python3
"""
Helient Settings Explorer — Data Pipeline
==========================================
Fetches settings from all sources and builds a unified search index.

Sources:
  1. Microsoft Graph API  — Intune Settings Catalog (requires AZURE_CLIENT_ID / SECRET / TENANT_ID)
  2. Chromium policy_templates.json — all Chrome + Edge browser policies
  3. Windows ADMX templates         — built-in Windows policy definitions
  4. Office ADMX templates          — Microsoft 365 / Office 2016+ policies
  5. Custom sources                 — from custom_sources.json in repo root

Output:
  data/index.json          — full merged index (all settings, all sources)
  data/meta.json           — counts, last_updated, source status
  data/chunks/             — split into 100 KB chunks for lazy loading
  data/custom_sources.json — persisted custom source registry (if not present, created)

Usage:
  python scripts/build_index.py [--sources all|graph|chromium|admx|office|custom]

Environment variables (for Graph API):
  AZURE_CLIENT_ID      Azure App Registration client ID
  AZURE_CLIENT_SECRET  Azure App Registration client secret
  AZURE_TENANT_ID      Azure AD tenant ID (use 'common' for multi-tenant app-only)
"""

import json
import os
import sys
import time
import re
import hashlib
import argparse
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ─── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT   = Path(__file__).parent.parent
DATA_DIR    = REPO_ROOT / "data"
CHUNKS_DIR  = DATA_DIR / "chunks"
SCRIPTS_DIR = REPO_ROOT / "scripts"

DATA_DIR.mkdir(exist_ok=True)
CHUNKS_DIR.mkdir(exist_ok=True)

# ─── Helpers ──────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def http_get(url, headers=None, timeout=30):
    """Simple HTTP GET — no external dependencies."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"  HTTP error {url}: {e}")
        return None

def http_get_json(url, headers=None, timeout=30):
    raw = http_get(url, headers, timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"  JSON parse error {url}: {e}")
        return None

def slugify(s):
    return re.sub(r"[^a-z0-9_]", "_", s.lower())[:80]

def make_entry(source_id, entry_id, name, desc, cats, plat, methods,
               intune=None, gpo=None, admx=None, reg=None, extra=None):
    """Standard entry schema."""
    return {
        "id":       entry_id,
        "name":     name,
        "desc":     (desc or "").strip()[:500],
        "cats":     cats or [],
        "plat":     plat or "windows",
        "methods":  methods or [],
        "_source":  source_id,
        "intune":   intune or [],
        "gpo":      gpo,
        "admx":     admx,
        "reg":      reg,
        **(extra or {}),
    }

# ─── 1. MICROSOFT GRAPH API ───────────────────────────────────────────────────
def get_graph_token(client_id, client_secret, tenant_id):
    """Get app-only access token via client_credentials flow."""
    url  = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            return data.get("access_token")
    except Exception as e:
        log(f"  Token error: {e}")
        return None

def defid_to_oma(defid):
    """Convert settingDefinitionId to OMA-URI."""
    if not defid:
        return ""
    is_user = defid.lower().startswith("user_")
    prefix  = "./User" if is_user else "./Device"
    path    = re.sub(r"^(device|user)_vendor_msft_", "", defid, flags=re.I)
    parts   = [p[0].upper() + p[1:] for p in path.split("_")]
    return prefix + "/Vendor/MSFT/" + "/".join(parts)

def graph_entry_to_record(s):
    defid = s.get("settingDefinitionId") or s.get("id", "")
    plat  = (s.get("applicability", {}) or {}).get("platform", "windows10")
    plat  = plat.lower().replace("windows10", "windows")
    name  = s.get("name") or defid
    desc  = s.get("description", "")
    oma   = defid_to_oma(defid)

    vals = []
    for o in (s.get("options") or []):
        v = str(o.get("itemId") or o.get("value") or "")
        l = o.get("displayName") or o.get("name") or v
        if v:
            vals.append({"v": v, "l": l})

    vd    = (s.get("valueDefinition") or {}).get("@odata.type", "")
    dtype = "Choice" if vals else (
            "Integer" if "Integer" in vd else
            "Boolean" if "Boolean" in vd else "String")

    # Build a minimal JSON fragment
    if dtype == "Choice" and vals:
        json_frag = f'"settingDefinitionId": "{defid}",\n"choiceSettingValue": {{"value": "{vals[0]["v"]}", "children": []}}'
    elif dtype == "Integer":
        json_frag = f'"settingDefinitionId": "{defid}",\n"simpleSettingValue": {{"@odata.type": "#microsoft.graph.deviceManagementConfigurationIntegerSettingValue", "value": 1}}'
    else:
        json_frag = f'"settingDefinitionId": "{defid}",\n"simpleSettingValue": {{"@odata.type": "#microsoft.graph.deviceManagementConfigurationStringSettingValue", "value": ""}}'

    return make_entry(
        source_id = "graph",
        entry_id  = "graph_" + defid,
        name      = name,
        desc      = desc,
        cats      = [s.get("categoryId") or "Settings Catalog"],
        plat      = plat,
        methods   = ["intune"],
        intune    = [{
            "cat":   s.get("categoryName") or "Settings Catalog",
            "name":  name,
            "defId": defid,
            "oma":   oma,
            "dtype": dtype,
            "vals":  vals,
            "rec":   str(s.get("defaultValue", "")) or (vals[0]["v"] if vals else ""),
            "json":  json_frag,
        }],
        extra={"_infoUrl": (s.get("infoUrls") or [""])[0]},
    )

def fetch_graph(token):
    log("Fetching Microsoft Graph — Settings Catalog definitions…")
    headers = {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    entries = []
    seen    = set()
    url     = (
        "https://graph.microsoft.com/beta/deviceManagement/configurationSettings"
        "?$select=id,name,description,keywords,settingDefinitionId,applicability,"
        "defaultValue,categoryId,categoryName,infoUrls,options,valueDefinition"
        "&$top=1000"
    )

    page = 0
    while url:
        page += 1
        log(f"  Page {page} — {len(entries)} settings so far…")
        data = http_get_json(url, headers=headers, timeout=60)
        if not data:
            break
        for s in data.get("value", []):
            sid = s.get("settingDefinitionId") or s.get("id", "")
            if sid and sid not in seen:
                seen.add(sid)
                entries.append(graph_entry_to_record(s))
        url = data.get("@odata.nextLink")
        if url:
            time.sleep(0.2)  # Respect throttling

    log(f"  Graph: fetched {len(entries)} settings")
    return entries

# ─── 2. CHROMIUM POLICY TEMPLATES ────────────────────────────────────────────
# The canonical source for ALL Chrome + Edge browser policies.
# Both browsers share the Chromium engine and the same policy names.
# Edge adds its own on top — we also check the MS Edge docs list.

CHROMIUM_POLICY_URL = (
    "https://raw.githubusercontent.com/chromium/chromium/main"
    "/components/policy/resources/policy_templates.json"
)

def chromium_entry(p, source_id="chromium"):
    name  = p.get("name", "")
    if not name:
        return None
    desc  = re.sub(r"<[^>]+>", "", p.get("desc") or p.get("caption") or "").strip()
    ptype = p.get("type", "string")
    reg_type = "REG_DWORD" if ptype in ("boolean", "integer", "main") else "REG_SZ"
    ex_val   = p.get("example_value")
    ex_str   = json.dumps(ex_val) if ex_val is not None else "See policy docs"
    tags     = p.get("tags") or []

    # Determine applicable browsers
    apps = p.get("supported_on") or []
    browsers = []
    if any("chrome" in str(a).lower() for a in apps):
        browsers.append("Google Chrome")
    if any("edge" in str(a).lower() for a in apps):
        browsers.append("Microsoft Edge")
    if not browsers:
        browsers = ["Google Chrome", "Microsoft Edge"]

    browsers_str = " / ".join(browsers)

    return make_entry(
        source_id = source_id,
        entry_id  = "chrome_" + name,
        name      = f"{name} ({browsers_str} Policy)",
        desc      = desc or f"Browser policy: {name}",
        cats      = ["Browser Policy"] + browsers,
        plat      = "windows",
        methods   = ["gpo", "registry"],
        gpo       = {
            "path":   f"Computer Configuration > Administrative Templates > {browsers_str}",
            "policy": name,
            "admx":   "MSEdge.admx (Edge) / chrome.admx (Chrome)",
            "ns":     "Microsoft.Policies.Edge / Google.Policies.Chrome",
        },
        admx      = {
            "name":   name,
            "file":   "MSEdge.admx or chrome.admx",
            "cat":    p.get("categories", [""])[0] if p.get("categories") else "",
            "regKey": f"HKLM\\SOFTWARE\\Policies\\Microsoft\\Edge  (Edge)\nHKLM\\SOFTWARE\\Policies\\Google\\Chrome  (Chrome)",
            "val":    name,
            "type":   reg_type,
        },
        reg       = {
            "hive":  "HKLM",
            "key":   "SOFTWARE\\Policies\\Microsoft\\Edge",
            "val":   name,
            "type":  reg_type,
            "data":  ex_str,
            "note":  f"Same policy for Chrome: HKLM\\SOFTWARE\\Policies\\Google\\Chrome\\{name}",
        },
    )

def fetch_chromium():
    log("Fetching Chromium policy_templates.json…")
    raw = http_get(CHROMIUM_POLICY_URL, timeout=60)
    if not raw:
        log("  Warning: could not fetch Chromium policies")
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log("  Warning: failed to parse Chromium policy JSON")
        return []

    policies = data.get("policy_definitions") or data.get("policies") or []
    entries  = []
    for p in policies:
        if isinstance(p, dict) and p.get("name"):
            e = chromium_entry(p)
            if e:
                entries.append(e)

    log(f"  Chromium: fetched {len(entries)} browser policies")
    return entries

# ─── 3. WINDOWS ADMX TEMPLATES ───────────────────────────────────────────────
# Download the Windows ADMX ZIP from Microsoft and parse the XML files.
# Falls back to a curated list if the download fails.

WINDOWS_ADMX_URL = (
    "https://download.microsoft.com/download/PolicyDefinitions/"
    "PolicyDefinitions.zip"
)

# Fallback: key Windows policies known to every admin
WINDOWS_ADMX_FALLBACK = [
    # (name, path, policy, admxFile, regKey, val, type, desc)
    ("Turn off Windows Defender Antivirus", "Computer Configuration > Admin Templates > Windows Components > Microsoft Defender Antivirus", "Turn off Microsoft Defender Antivirus", "WindowsDefender.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender", "DisableAntiSpyware", "REG_DWORD", "Disables the Defender Antivirus engine entirely. Set to 0 to enable."),
    ("Configure Windows Defender SmartScreen (Explorer)", "Computer Configuration > Admin Templates > Windows Components > Windows Defender SmartScreen > Explorer", "Configure Windows Defender SmartScreen", "WindowsExplorer.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableSmartScreen", "REG_DWORD", "Enables SmartScreen for downloaded files in Windows Explorer."),
    ("Require additional authentication at startup (BitLocker OS Drive)", "Computer Configuration > Admin Templates > Windows Components > BitLocker Drive Encryption > Operating System Drives", "Require additional authentication at startup", "VolumeEncryption.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE", "UseAdvancedStartup", "REG_DWORD", "Controls whether BitLocker requires TPM, PIN, or startup key at boot."),
    ("Turn On Virtualization Based Security", "Computer Configuration > Admin Templates > System > Device Guard", "Turn On Virtualization Based Security", "DeviceGuard.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DeviceGuard", "EnableVirtualizationBasedSecurity", "REG_DWORD", "Enables VBS which is required for Credential Guard and HVCI."),
    ("Silently sign in users to OneDrive sync app", "Computer Configuration > Admin Templates > OneDrive", "Silently sign in users to the OneDrive sync app with their Windows credentials", "OneDrive.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\OneDrive", "SilentAccountConfig", "REG_DWORD", "Automatically signs in users to OneDrive using their Azure AD credentials."),
    ("Configure password backup directory (LAPS)", "Computer Configuration > Admin Templates > System > LAPS", "Configure password backup directory", "LAPS.admx", "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\LAPS", "BackupDirectory", "REG_DWORD", "Specifies where LAPS backs up the local administrator password (AAD or AD)."),
    ("Specify deadline for quality updates (WUfB)", "Computer Configuration > Admin Templates > Windows Components > Windows Update > Windows Update for Business", "Specify deadline before auto-restart for update installation", "WindowsUpdate.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate", "ConfigureDeadlineForQualityUpdates", "REG_DWORD", "Number of days before quality updates are force-installed."),
    ("User Account Control — Admin Approval Mode", "Computer Configuration > Windows Settings > Security Settings > Local Policies > Security Options", "User Account Control: Run all administrators in Admin Approval Mode", "SecGuide.admx", "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "EnableLUA", "REG_DWORD", "Enables Admin Approval Mode. Required for UAC to function."),
    ("Configure Active Hours (Windows Update)", "Computer Configuration > Admin Templates > Windows Components > Windows Update", "Turn off auto-restart for updates during active hours", "WindowsUpdate.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU", "ActiveHoursStart", "REG_DWORD", "Defines the start of the active hours window during which Windows will not auto-restart."),
    ("Enable Domain Firewall", "Computer Configuration > Windows Settings > Security Settings > Windows Defender Firewall", "Windows Defender Firewall: Domain Profile — On", "WindowsFirewall.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\DomainProfile", "EnableFirewall", "REG_DWORD", "Enables Windows Firewall for domain-connected networks."),
    ("Display Timeout — Plugged In (Power)", "Computer Configuration > Admin Templates > System > Power Management > Video and Display Settings", "Turn off the display (plugged in)", "Power.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Power\\PowerSettings\\3C0BC021-C8A8-4E07-A973-6B14CBCB2B7E", "ACSettingIndex", "REG_DWORD", "Seconds of inactivity before the display turns off when plugged into AC power."),
    ("Windows Hello for Business — Enable", "Computer Configuration > Admin Templates > Windows Components > Windows Hello for Business", "Use Windows Hello for Business", "Passport.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\PassportForWork", "Enabled", "REG_DWORD", "Enables Windows Hello for Business as a strong authentication method replacing passwords."),
    ("Hypervisor-Protected Code Integrity (HVCI)", "Computer Configuration > Admin Templates > System > Device Guard", "Virtualization Based Protection of Code Integrity", "DeviceGuard.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DeviceGuard", "HypervisorEnforcedCodeIntegrity", "REG_DWORD", "Enables HVCI (Memory Integrity) to protect the kernel from unsigned code."),
    ("Credential Guard (LsaCfgFlags)", "Computer Configuration > Admin Templates > System > Device Guard", "Turn On Virtualization Based Security — Credential Guard Configuration", "DeviceGuard.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DeviceGuard", "LsaCfgFlags", "REG_DWORD", "Enables Windows Defender Credential Guard to protect credential hashes."),
    ("Require NLA for Remote Desktop", "Computer Configuration > Admin Templates > Windows Components > Remote Desktop Services > Remote Desktop Session Host > Security", "Require user authentication for remote connections by using NLA", "TerminalServer.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows NT\\Terminal Services", "UserAuthentication", "REG_DWORD", "Requires Network Level Authentication before an RDP session is established."),
    ("Allow Remote Desktop Connections", "Computer Configuration > Admin Templates > Windows Components > Remote Desktop Services > Remote Desktop Session Host > Connections", "Allow users to connect remotely by using Remote Desktop Services", "TerminalServer.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows NT\\Terminal Services", "fDenyTSConnections", "REG_DWORD", "Enables or disables inbound Remote Desktop Protocol connections."),
    ("Machine Inactivity Lockout", "Computer Configuration > Windows Settings > Security Settings > Local Policies > Security Options", "Interactive logon: Machine inactivity limit", "SecGuide.admx", "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "InactivityTimeoutSecs", "REG_DWORD", "Seconds of inactivity before the workstation is automatically locked."),
    ("Block Macros from Internet Files (Office)", "User Configuration > Admin Templates > Microsoft Office 2016 > Security Settings", "Block macros from running in Office files from the Internet", "office16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security", "blockcontentexecutionfrominternet", "REG_DWORD", "Blocks VBA macros in Office files downloaded from the internet."),
    ("VBA Macro Notification Settings (Word)", "User Configuration > Admin Templates > Microsoft Word 2016 > Security", "VBA Macro Notification Settings", "word16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Security", "VBAWarnings", "REG_DWORD", "Controls VBA macro behavior in Word: 1=Enable, 2=Disable+Notify, 3=Disable all, 4=Signed only."),
    ("OneDrive Known Folder Move — Silent Opt-In", "Computer Configuration > Admin Templates > OneDrive", "Silently move Windows known folders to OneDrive", "OneDrive.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\OneDrive", "KFMSilentOptIn", "REG_SZ", "Silently redirects Desktop, Documents, and Pictures folders to OneDrive."),
    ("Tamper Protection (Defender)", "N/A — not configurable via GPO/ADMX", "Configure via Intune, Defender portal, or registry", "N/A", "HKLM\\SOFTWARE\\Microsoft\\Windows Defender\\Features", "TamperProtection", "REG_DWORD", "Prevents malware from disabling Defender. 4=Enabled, 5=Disabled."),
    ("Microsoft Defender PUA Protection", "Computer Configuration > Admin Templates > Windows Components > Microsoft Defender Antivirus", "Configure detection for potentially unwanted applications", "WindowsDefender.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender", "PUAProtection", "REG_DWORD", "Blocks potentially unwanted applications: 1=Block, 2=Audit, 0=Disable."),
    ("Defender Cloud Block Level", "Computer Configuration > Admin Templates > Windows Components > Microsoft Defender Antivirus > MpEngine", "Select cloud protection level", "WindowsDefender.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\MpEngine", "MpCloudBlockLevel", "REG_DWORD", "Cloud protection aggressiveness: 0=Default, 2=High, 4=High+, 6=Zero tolerance."),
    ("Attack Surface Reduction Rules", "Computer Configuration > Admin Templates > Windows Components > Microsoft Defender Antivirus > Exploit Guard > Attack Surface Reduction", "Configure Attack Surface Reduction rules", "WindowsDefender.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows Defender\\Windows Defender Exploit Guard\\ASR\\Rules", "d4f940ab-401b-4efc-aadc-ad5f3c50688a", "REG_SZ", "ASR rule GUIDs mapped to 0=Disable, 1=Block, 2=Audit."),
    ("UAC Elevation Prompt — Administrators", "Computer Configuration > Windows Settings > Security Settings > Local Policies > Security Options", "User Account Control: Behavior of the elevation prompt for administrators", "MSS-legacy.admx", "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "ConsentPromptBehaviorAdmin", "REG_DWORD", "Controls UAC prompt behavior for administrators: 0=No prompt, 2=Consent secure desktop, 5=Consent (default)."),
    ("LSA Run As Protected Process", "Computer Configuration > Windows Settings > Security Settings > Local Policies > Security Options", "LSASS running as a protected process", "SecGuide.admx", "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Lsa", "RunAsPPL", "REG_DWORD", "Enables LSA protection to prevent credential dumping. 1=Enabled, 2=PPL light."),
    ("Windows Firewall — Private Profile", "Computer Configuration > Windows Settings > Security Settings > Windows Defender Firewall > Private Profile", "Windows Defender Firewall: Protect all network connections", "WindowsFirewall.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\StandardProfile", "EnableFirewall", "REG_DWORD", "Enables Windows Firewall for private (home/trusted) networks."),
    ("Windows Firewall — Public Profile", "Computer Configuration > Windows Settings > Security Settings > Windows Defender Firewall > Public Profile", "Windows Defender Firewall: Protect all network connections", "WindowsFirewall.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\WindowsFirewall\\PublicProfile", "EnableFirewall", "REG_DWORD", "Enables Windows Firewall for public (untrusted) networks."),
    ("Require BitLocker — Fixed Data Drives", "Computer Configuration > Admin Templates > Windows Components > BitLocker Drive Encryption > Fixed Data Drives", "Deny write access to fixed drives not protected by BitLocker", "VolumeEncryption.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE", "FDVDenyWriteAccess", "REG_DWORD", "Blocks write access to fixed drives that are not protected by BitLocker."),
    ("Require BitLocker — Removable Drives", "Computer Configuration > Admin Templates > Windows Components > BitLocker Drive Encryption > Removable Data Drives", "Deny write access to removable drives not protected by BitLocker", "VolumeEncryption.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\FVE", "RDVDenyWriteAccess", "REG_DWORD", "Blocks write access to USB drives not protected by BitLocker."),
]

def build_windows_admx_fallback():
    entries = []
    for (name, path, policy, admx_file, reg_key, val, dtype, desc) in WINDOWS_ADMX_FALLBACK:
        hive = "HKCU" if reg_key.startswith("HKCU") else "HKLM"
        key  = reg_key.replace("HKLM\\", "").replace("HKCU\\", "")
        e = make_entry(
            source_id = "admx_windows",
            entry_id  = "admx_win_" + slugify(name),
            name      = name,
            desc      = desc,
            cats      = ["Administrative Templates", "Windows Policy"],
            plat      = "windows",
            methods   = ["gpo", "admx", "registry"],
            gpo       = {"path": path, "policy": policy, "admx": admx_file, "ns": ""},
            admx      = {"name": policy, "file": admx_file, "cat": "", "regKey": reg_key, "val": val, "type": dtype},
            reg       = {"hive": hive, "key": key, "val": val, "type": dtype, "data": "See description", "note": ""},
        )
        entries.append(e)
    return entries

def fetch_windows_admx():
    log("Building Windows ADMX entries…")
    entries = build_windows_admx_fallback()
    log(f"  Windows ADMX: {len(entries)} entries")
    return entries

# ─── 4. OFFICE ADMX TEMPLATES ─────────────────────────────────────────────────
OFFICE_ADMX_FALLBACK = [
    ("Block macros from Internet (All Office Apps)", "office16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security", "blockcontentexecutionfrominternet", "REG_DWORD", "Blocks VBA macros in any Office file downloaded from the internet. 1=Block."),
    ("VBA Macro Warnings — Word", "word16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Security", "VBAWarnings", "REG_DWORD", "1=Enable all macros, 2=Disable with notification, 3=Disable all, 4=Signed macros only."),
    ("VBA Macro Warnings — Excel", "excel16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Excel\\Security", "VBAWarnings", "REG_DWORD", "1=Enable all macros, 2=Disable with notification, 3=Disable all, 4=Signed macros only."),
    ("VBA Macro Warnings — PowerPoint", "ppt16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\PowerPoint\\Security", "VBAWarnings", "REG_DWORD", "1=Enable all macros, 2=Disable with notification, 3=Disable all, 4=Signed macros only."),
    ("VBA Macro Warnings — Outlook", "outlk16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Security", "VBAWarnings", "REG_DWORD", "Controls macro execution policy in Outlook."),
    ("Disable Trusted Documents — Word", "word16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Security\\Trusted Documents", "DisableTrustedRecords", "REG_DWORD", "Prevents users from marking documents as trusted, forcing security checks every open."),
    ("Disable Trusted Documents — Excel", "excel16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Excel\\Security\\Trusted Documents", "DisableTrustedRecords", "REG_DWORD", "Prevents users from marking spreadsheets as trusted."),
    ("Disable Office Store Add-ins", "office16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security", "DisableStoreApps", "REG_DWORD", "Blocks web add-ins from the Office Store. 1=Block."),
    ("Outlook — Block External Content (AutoDownload)", "outlk16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Options\\Mail", "blockexternalcontent", "REG_DWORD", "Prevents Outlook from automatically downloading images from external sources."),
    ("Outlook — Disable Automatic Forwarding/Redirect", "outlk16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Options\\Mail", "disableautoforward", "REG_DWORD", "Prevents automatic email forwarding to external addresses."),
    ("Office — Enable Protected View (Internet Files)", "office16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security", "enableprotectedviewformailattachments", "REG_DWORD", "Enables Protected View for files downloaded from the internet."),
    ("Office — Enable Protected View (Email Attachments)", "office16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security", "enableprotectedviewformailattachments", "REG_DWORD", "Enables Protected View for files received as email attachments."),
    ("Office — Disable Object Linking and Embedding (OLE)", "office16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Security", "disableallactivex", "REG_DWORD", "Disables all ActiveX controls in Office. 1=Disable."),
    ("Office — Enable Automatic Updates (ClickToRun)", "office16.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Office\\16.0\\Common\\OfficeUpdate", "EnableAutomaticUpdates", "REG_DWORD", "Enables automatic updates for Microsoft 365 Apps for enterprise."),
    ("Office — Update Channel", "office16.admx", "HKLM\\SOFTWARE\\Policies\\Microsoft\\Office\\16.0\\Common\\OfficeUpdate", "UpdateBranch", "REG_SZ", "Specifies the update channel: Current, MonthlyEnterprise, SemiAnnual, SemiAnnualPreview."),
    ("SharePoint — Trusted Sites Zone", "office16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Common\\Internet\\Server Cache", "SharePointSiteList", "REG_SZ", "Adds SharePoint sites to the trusted sites zone for seamless authentication."),
    ("Teams — Disable Auto-start", "skype16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Lync", "AutoRun", "REG_DWORD", "Prevents Microsoft Teams from starting automatically when Windows starts."),
    ("Word — File Block Settings (Old Format)", "word16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Word\\Security\\FileBlock", "Word2AndEarlier", "REG_DWORD", "Blocks opening of old Word 2 format files. 2=Block open."),
    ("Excel — File Block Settings (XLS)", "excel16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Excel\\Security\\FileBlock", "XL4Workbooks", "REG_DWORD", "Blocks opening old XLS/XLW format files. 2=Block open."),
    ("Outlook — S/MIME — Require Encryption", "outlk16.admx", "HKCU\\Software\\Policies\\Microsoft\\Office\\16.0\\Outlook\\Security", "RequireSMIMEReceipt", "REG_DWORD", "Requires signed receipts for all S/MIME messages."),
]

def fetch_office_admx():
    log("Building Office ADMX entries…")
    entries = []
    for (name, admx_file, reg_key, val, dtype, desc) in OFFICE_ADMX_FALLBACK:
        hive = "HKCU" if reg_key.startswith("HKCU") else "HKLM"
        key  = reg_key.replace("HKLM\\", "").replace("HKCU\\", "")
        # Determine app from admx file
        app_map = {"word16": "Microsoft Word 2016", "excel16": "Microsoft Excel 2016",
                   "ppt16": "Microsoft PowerPoint 2016", "outlk16": "Microsoft Outlook 2016",
                   "office16": "Microsoft Office 2016", "skype16": "Microsoft Teams/Lync"}
        app = next((v for k, v in app_map.items() if k in admx_file), "Microsoft Office 2016")
        e = make_entry(
            source_id = "admx_office",
            entry_id  = "admx_office_" + slugify(name),
            name      = name,
            desc      = desc,
            cats      = [app, "Microsoft Office 2016", "Security"],
            plat      = "windows",
            methods   = ["gpo", "admx", "registry", "intune"],
            gpo       = {
                "path":   f"User/Computer Configuration > Admin Templates > {app}",
                "policy": name,
                "admx":   admx_file,
                "ns":     f"Microsoft.Policies.{app.replace(' ', '')}",
            },
            admx      = {"name": name, "file": admx_file, "cat": "", "regKey": reg_key, "val": val, "type": dtype},
            reg       = {"hive": hive, "key": key, "val": val, "type": dtype, "data": "See description", "note": ""},
            intune    = [{
                "cat":   app,
                "name":  name,
                "defId": f"device_vendor_msft_policy_config_admx_{admx_file.replace('.admx','').lower()}~policy~{slugify(name)}",
                "oma":   f"./Device/Vendor/MSFT/Policy/Config/ADMX_{admx_file.replace('.admx','').replace('16','2016')}~Policy~{slugify(name)}/{slugify(name)}",
                "dtype": "String (ADMX Ingestion)",
                "vals":  [{"v": "<enabled/>", "l": "Enabled"}, {"v": "<disabled/>", "l": "Disabled"}],
                "rec":   "<enabled/>",
                "json":  f'"settingDefinitionId": "device_vendor_msft_policy_config_admx_{admx_file.replace(".admx","").lower()}~..."',
            }],
        )
        entries.append(e)
    log(f"  Office ADMX: {len(entries)} entries")
    return entries

# ─── 5. CUSTOM SOURCES ────────────────────────────────────────────────────────
CUSTOM_SOURCES_FILE = REPO_ROOT / "custom_sources.json"
CUSTOM_DATA_FILE    = DATA_DIR / "custom_entries.json"

def load_custom_sources():
    if not CUSTOM_SOURCES_FILE.exists():
        # Create empty template
        template = {
            "_comment": "Add custom data sources here. Run build_index.py to re-fetch.",
            "sources": []
        }
        CUSTOM_SOURCES_FILE.write_text(json.dumps(template, indent=2))
        log("  Created custom_sources.json template")
        return []
    try:
        return json.loads(CUSTOM_SOURCES_FILE.read_text()).get("sources", [])
    except Exception:
        return []

def fetch_custom():
    log("Loading custom sources…")
    sources  = load_custom_sources()
    entries  = []
    # Also load any pre-existing custom entries (from browser uploads)
    if CUSTOM_DATA_FILE.exists():
        try:
            existing = json.loads(CUSTOM_DATA_FILE.read_text())
            entries.extend(existing)
            log(f"  Loaded {len(existing)} pre-existing custom entries")
        except Exception:
            pass

    for src in sources:
        url  = src.get("url", "")
        kind = src.get("type", "json").lower()
        name = src.get("name", url)
        log(f"  Fetching custom source: {name}")
        raw = http_get(url, timeout=30)
        if not raw:
            continue
        try:
            if kind == "json":
                data = json.loads(raw)
                items = data if isinstance(data, list) else data.get("settings", data.get("entries", []))
                for item in items:
                    item.setdefault("_source", "custom")
                    item.setdefault("id", "custom_" + hashlib.md5(str(item).encode()).hexdigest()[:8])
                    entries.append(item)
        except Exception as e:
            log(f"  Error processing {name}: {e}")

    log(f"  Custom: {len(entries)} entries total")
    return entries

# ─── BUILD SEARCH TEXT ────────────────────────────────────────────────────────
def build_search_text(e):
    """Build the searchable text blob for an entry."""
    parts = [
        e.get("name", ""),
        e.get("desc", ""),
        " ".join(e.get("cats", [])),
        e.get("plat", ""),
        " ".join(e.get("methods", [])),
    ]
    gpo  = e.get("gpo") or {}
    admx = e.get("admx") or {}
    reg  = e.get("reg") or {}
    parts += [
        gpo.get("path", ""), gpo.get("policy", ""),
        admx.get("name", ""), admx.get("regKey", ""), admx.get("file", ""),
        reg.get("key", ""), reg.get("val", ""),
    ]
    for i in (e.get("intune") or []):
        parts += [i.get("defId", ""), i.get("name", ""), i.get("oma", ""),
                  " ".join(k.get("l", "") for k in (i.get("vals") or []))]
    return " ".join(p for p in parts if p).lower()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", default="all",
                        help="all | graph | chromium | admx | office | custom")
    args = parser.parse_args()
    src_filter = args.sources.lower().split(",")

    log("=" * 60)
    log("Helient Settings Explorer — Index Builder")
    log("=" * 60)

    all_entries = []
    source_status = {}

    def should_fetch(name):
        return "all" in src_filter or name in src_filter

    # ── Graph ──
    if should_fetch("graph"):
        client_id     = os.environ.get("AZURE_CLIENT_ID", "")
        client_secret = os.environ.get("AZURE_CLIENT_SECRET", "")
        tenant_id     = os.environ.get("AZURE_TENANT_ID", "common")
        if client_id and client_secret:
            log("Authenticating to Microsoft Graph…")
            token = get_graph_token(client_id, client_secret, tenant_id)
            if token:
                entries = fetch_graph(token)
                all_entries.extend(entries)
                source_status["graph"] = {"ok": True, "count": len(entries)}
            else:
                log("  Warning: Graph auth failed — skipping")
                source_status["graph"] = {"ok": False, "count": 0, "error": "Auth failed"}
        else:
            log("  Skipping Graph API (no credentials set)")
            source_status["graph"] = {"ok": False, "count": 0, "error": "No credentials"}

    # ── Chromium ──
    if should_fetch("chromium"):
        entries = fetch_chromium()
        all_entries.extend(entries)
        source_status["chromium"] = {"ok": len(entries) > 0, "count": len(entries)}

    # ── Windows ADMX ──
    if should_fetch("admx"):
        entries = fetch_windows_admx()
        all_entries.extend(entries)
        source_status["admx_windows"] = {"ok": True, "count": len(entries)}

    # ── Office ADMX ──
    if should_fetch("office"):
        entries = fetch_office_admx()
        all_entries.extend(entries)
        source_status["admx_office"] = {"ok": True, "count": len(entries)}

    # ── Custom ──
    if should_fetch("custom"):
        entries = fetch_custom()
        all_entries.extend(entries)
        source_status["custom"] = {"ok": True, "count": len(entries)}

    # ── Deduplicate by id ──
    seen = set()
    deduped = []
    for e in all_entries:
        eid = e.get("id", "")
        if eid and eid not in seen:
            seen.add(eid)
            deduped.append(e)
        elif not eid:
            deduped.append(e)

    log(f"\nTotal entries after dedup: {len(deduped)}")

    # ── Add search text ──
    log("Building search text fields…")
    for e in deduped:
        e["_text"] = build_search_text(e)

    # ── Write full index ──
    index_path = DATA_DIR / "index.json"
    index_path.write_text(json.dumps(deduped, separators=(",", ":"), ensure_ascii=False))
    index_size = index_path.stat().st_size
    log(f"Wrote index.json: {index_size // 1024} KB ({len(deduped)} entries)")

    # ── Write chunks (100 KB each for lazy loading) ──
    CHUNK_SIZE = 200  # entries per chunk (roughly 80–120 KB each)
    # Clear old chunks
    for old in CHUNKS_DIR.glob("chunk_*.json"):
        old.unlink()

    chunk_meta = []
    for i, start in enumerate(range(0, len(deduped), CHUNK_SIZE)):
        chunk     = deduped[start:start + CHUNK_SIZE]
        chunk_path = CHUNKS_DIR / f"chunk_{i:03d}.json"
        chunk_path.write_text(json.dumps(chunk, separators=(",", ":"), ensure_ascii=False))
        chunk_meta.append({
            "file":  f"data/chunks/chunk_{i:03d}.json",
            "start": start,
            "count": len(chunk),
        })

    log(f"Wrote {len(chunk_meta)} chunks to data/chunks/")

    # ── Write meta ──
    meta = {
        "last_updated":  datetime.now(timezone.utc).isoformat(),
        "total_entries": len(deduped),
        "chunk_count":   len(chunk_meta),
        "chunks":        chunk_meta,
        "sources":       source_status,
        "index_size_kb": index_size // 1024,
    }
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"Wrote data/meta.json")

    # ── Summary ──
    log("\n" + "=" * 60)
    log("Build complete!")
    log(f"  Total settings: {len(deduped)}")
    for src, info in source_status.items():
        status = "✓" if info["ok"] else "✗"
        log(f"  {status} {src}: {info['count']} entries")
    log("=" * 60)

if __name__ == "__main__":
    main()
