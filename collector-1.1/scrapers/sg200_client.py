#!/usr/bin/env python3
"""
Library to scrape data from a Cisco SG200 (HTTP-only management UI).

Public API:
    fetch_mac_table(switch_ip, username, password) -> List[dict]
    fetch_system_summary(switch_ip, username, password) -> Dict[str, str]

Implementation notes:
- Uses Playwright (headless Chromium) because SG200 auth and navigation are JS/frameset-based.
- Uses the csbXXXXXX URL prefix detected after login.
"""

import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

NAV_TIMEOUT_MS = 15000  # 15 seconds


@dataclass
class MacEntry:
    vlan: int
    mac: str
    port_index: int
    switch_ip: str


# -----------------------------
# Core helpers
# -----------------------------

def _perform_login(page, username: str, password: str) -> None:
    """
    Find a frame with a password field, fill username/password, submit.

    SG200 firmwares commonly render login inside a frame.
    """
    login_frame = None
    for frame in page.frames:
        try:
            pw = frame.query_selector("input[type='password']")
        except Exception:
            pw = None
        if pw is not None:
            login_frame = frame
            break

    if login_frame is None:
        # Already logged in or different firmware behavior
        return

    pw = login_frame.query_selector("input[type='password']")
    if pw is None:
        raise RuntimeError("Password field not found in login frame")

    user = login_frame.query_selector("input[type='text'], input[type='email']")
    if user is None:
        user = login_frame.query_selector("input:not([type='password'])")
    if user is None:
        raise RuntimeError("Could not locate username field on login page")

    user.fill(username)
    pw.fill(password)

    btn = login_frame.query_selector("input[type='submit'], button, input[type='button']")
    if btn is not None:
        btn.click()
    else:
        pw.press("Enter")

    # Allow frameset to load after auth
    page.wait_for_timeout(4000)


def _detect_csb_prefix(page) -> str:
    """
    After login, inspect frame URLs to find /csbXXXXXX/.
    """
    pattern = re.compile(r"/(csb[0-9a-fA-F]+)/")

    for frame in page.frames:
        m = pattern.search(frame.url or "")
        if m:
            return m.group(1)

    m = pattern.search(page.url or "")
    if m:
        return m.group(1)

    raise RuntimeError("Could not detect csbXXXXXX prefix after login")


def _format_mac(mac_hex: str) -> str:
    mac_hex = (mac_hex or "").strip().lower()
    if len(mac_hex) == 12 and all(c in "0123456789abcdef" for c in mac_hex):
        return ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2))
    if len(mac_hex) % 2 == 0 and len(mac_hex) >= 12:
        return ":".join(mac_hex[i:i + 2] for i in range(0, len(mac_hex), 2))
    return mac_hex


def _parse_dynamic_mac_table(html: str, switch_ip: str) -> List[MacEntry]:
    """
    Parse VLAN / MAC / port entries from the Dynamic MAC page HTML.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    inputs = soup.find_all("input")

    vlan_by_idx: Dict[str, int] = {}
    mac_by_idx: Dict[str, str] = {}
    port_by_idx: Dict[str, int] = {}

    for inp in inputs:
        name = inp.get("name")
        if not name:
            continue
        value = (inp.get("value") or "").strip()

        if name.startswith("dot1qFdbId$repeat?"):
            idx = name.split("?", 1)[1]
            try:
                vlan_by_idx[idx] = int(value)
            except ValueError:
                continue

        elif name.startswith("dot1qTpFdbAddress$repeat?"):
            idx = name.split("?", 1)[1]
            mac_by_idx[idx] = value

        elif name.startswith("dot1qTpFdbPort$repeat?"):
            idx = name.split("?", 1)[1]
            try:
                port_by_idx[idx] = int(value)
            except ValueError:
                continue

    entries: List[MacEntry] = []
    for idx in sorted(mac_by_idx.keys(), key=lambda x: int(x)):
        mac_hex = mac_by_idx.get(idx)
        vlan = vlan_by_idx.get(idx)
        port_index = port_by_idx.get(idx)

        if not mac_hex or vlan is None or port_index is None:
            continue

        entries.append(
            MacEntry(
                vlan=vlan,
                mac=_format_mac(mac_hex),
                port_index=port_index,
                switch_ip=switch_ip,
            )
        )

    return entries

def _parse_portdb_xml(xml_text: str) -> Dict[int, str]:
    """
    Parse /device/portDB.xml and return a mapping of ifIndex -> portName.
    """
    out: Dict[int, str] = {}
    if not xml_text:
        return out

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    for port in root.findall(".//port"):
        try:
            if_index_el = port.find("ifIndex")
            port_name_el = port.find("portName")
            if if_index_el is None or port_name_el is None:
                continue
            if_index = int((if_index_el.text or "").strip())
            port_name = (port_name_el.text or "").strip()
            if port_name:
                out[if_index] = port_name
        except Exception:
            continue
    return out


# -----------------------------
# System Summary discovery + parsing
# -----------------------------

def _looks_like_system_summary(html: str) -> bool:
    """
    SG200 System Summary / System Information pages are heavily tokenized.
    This heuristic checks for stable hidden input field names observed in HAR.
    """
    if not html:
        return False
    # Serial number / firmware version are very stable across SG200 family pages.
    return ("rlPhdUnitGenParamSerialNum$repeat?1" in html) or ("rlPhdUnitGenParamSwVer$repeat?1" in html)


def _get_input_value(soup: BeautifulSoup, name: str) -> Optional[str]:
    inp = soup.find("input", {"name": name})
    if not inp:
        return None
    val = inp.get("value")
    if val is None:
        return None
    val = str(val).strip()
    return val if val != "" else None

def _extract_default_value(vt_value: str) -> Optional[str]:
    """
    Some SG200 pages encode values inside a *$VT field, e.g.:
        "Type=100;...;Default value=DNI161702F3"
    """
    if not vt_value:
        return None
    m = re.search(r"Default value=([^;]+)", vt_value)
    if not m:
        return None
    return m.group(1).strip() or None


def _parse_system_summary(html: str) -> Dict[str, str]:
    """
    Parse system summary using hidden inputs (more robust than reading UI labels).

    This page varies across SG200 firmwares; we prefer stable hidden fields that exist on
    system_general_description_Sx200_m.htm (observed in HAR captures).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    out: Dict[str, str] = {}

    # SNMP-ish fields (present on multiple pages)
    sys_name = _get_input_value(soup, "sysName")
    sys_contact = _get_input_value(soup, "sysContact")
    sys_location = _get_input_value(soup, "sysLocation")

    if sys_name:
        out["host_name"] = sys_name.strip()
    if sys_contact:
        out["system_contact"] = sys_contact.strip()
    if sys_location:
        out["system_location"] = sys_location.strip()

    # Model description (sysDescr$scalar observed in HAR)
    model = (
        _get_input_value(soup, "sysDescr$scalar")
        or _get_input_value(soup, "sysDescr")
        or _get_input_value(soup, "rlPhdUnitGenParamDeviceDescr$repeat?1")
    )
    if model:
        out["model_description"] = " ".join(model.replace("\xa0", " ").split())

    # Firmware version (observed as rndImage1Version$repeat?1 / rndImage2Version$repeat?1)
    fw = (
        _get_input_value(soup, "rndImage1Version$repeat?1")
        or _get_input_value(soup, "rndImage2Version$repeat?1")
        or _get_input_value(soup, "rlPhdUnitGenParamSwVer$repeat?1")
    )
    if fw:
        out["firmware_version"] = fw.strip()

    # Serial number (observed in rlPhdUnitGenParamSerialNum$VT as "Default value=...")
    serial = _get_input_value(soup, "rlPhdUnitGenParamSerialNum$repeat?1") or _extract_default_value(
        _get_input_value(soup, "rlPhdUnitGenParamSerialNum$VT") or ""
    )
    if serial:
        out["serial_number"] = serial.strip()

    return out


def _find_system_summary_html(page, switch_ip: str, prefix: str) -> Optional[str]:
    """
    Locate the System Summary page HTML.

    Primary clue (from HAR): the System Summary content lives at:
        /<prefix>/sysinfo/system_general_description_Sx200_m.htm
    """
    candidates = [
        # HAR-observed "System Summary" payload carrier for SG200 family
        f"http://{switch_ip}/{prefix}/sysinfo/system_general_description_Sx200_m.htm",
        f"http://{switch_ip}/{prefix}/sysinfo/system_general_description_Sx200.htm",

        # Other plausible variants (seen across small business firmwares)
        f"http://{switch_ip}/{prefix}/sysinfo/system_general_description_m.htm",
        f"http://{switch_ip}/{prefix}/sysinfo/system_general_description.htm",
        f"http://{switch_ip}/{prefix}/sysinfo/system_information_m.htm",
        f"http://{switch_ip}/{prefix}/sysinfo/system_information.htm",

        # Older attempts
        f"http://{switch_ip}/{prefix}/sysinfo/system_summary_m.htm",
        f"http://{switch_ip}/{prefix}/sysinfo/system_summary.htm",
        f"http://{switch_ip}/{prefix}/sysinfo/systemSummary.htm",
        f"http://{switch_ip}/{prefix}/Status/system_summary_m.htm",
        f"http://{switch_ip}/{prefix}/Status/system_summary.htm",
    ]

    attempts: List[str] = []

    # 1) Try visiting home to populate frames
    home_url = f"http://{switch_ip}/{prefix}/home.htm"
    try:
        page.goto(home_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:
        pass

    # 2) Check any already-loaded frame contents
    try:
        for i, fr in enumerate(page.frames):
            try:
                html = fr.content()
            except Exception:
                continue
            if _looks_like_system_summary(html):
                return html
    except Exception:
        pass

    # 3) Try direct candidate URLs
    for url in candidates:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = getattr(resp, "status", None)
            attempts.append(f"{url} -> status={status}")
        except PlaywrightTimeoutError:
            attempts.append(f"{url} -> timeout")
        except Exception as e:
            attempts.append(f"{url} -> error={type(e).__name__}: {e}")
            continue

        try:
            html = page.content()
        except Exception:
            html = ""

        if _looks_like_system_summary(html):
            return html
    return None


# -----------------------------
# Public API
# -----------------------------

def fetch_mac_table(switch_ip: str, username: str, password: str) -> List[dict]:
    """
    Scrape the dynamic MAC table and return a list of dicts:

        [
          {"switch_ip": "192.168.0.221", "vlan": 1, "mac": "aa:bb:...", "port_index": "GE1"},
          ...
        ]

    Notes:
    - The Dynamic Addresses page stores the raw port index in hidden fields (dot1qTpFdbPort$repeat?X).
      The UI renders the human-friendly interface name (GE1/GE2/...) by looking up port names from
      device/portDB.xml. We replicate this by fetching portDB.xml (authenticated) and mapping ifIndex->portName.
    - If port name resolution fails, port_index will fall back to the raw numeric index as a string.
    """
    base_http_url = f"http://{switch_ip}/"

    result: List[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        try:
            try:
                page.goto(base_http_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass

            _perform_login(page, username, password)
            page.wait_for_timeout(3000)

            prefix = _detect_csb_prefix(page)

            # Fetch authenticated portDB.xml to map ifIndex -> portName (e.g., 73 -> GE1).
            port_name_by_ifindex: Dict[int, str] = {}
            try:
                portdb_url = f"http://{switch_ip}/{prefix}/device/portDB.xml?Filter:(ifOperStatus!=6)"
                resp = context.request.get(portdb_url, timeout=NAV_TIMEOUT_MS)
                if resp and resp.ok:
                    xml_text = resp.text()
                    port_name_by_ifindex = _parse_portdb_xml(xml_text)
            except Exception:
                port_name_by_ifindex = {}

            # Load Dynamic Addresses page and parse MAC entries.
            dyn_url = f"http://{switch_ip}/{prefix}/Adrs_tbl/bridg_frdData_dynamicAddress_m.htm"
            try:
                page.goto(dyn_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass

            dyn_html = page.content()
            entries = _parse_dynamic_mac_table(dyn_html, switch_ip)

            for e in entries:
                raw = e.port_index
                name = port_name_by_ifindex.get(raw)
                port_index = (name or str(raw)).strip()
                result.append(
                    {
                        "switch_ip": e.switch_ip,
                        "vlan": e.vlan,
                        "mac": e.mac,
                        "port_index": port_index,
                    }
                )

        finally:
            try:
                browser.close()
            except Exception:
                pass

    return result


def fetch_system_summary(switch_ip: str, username: str, password: str) -> Dict[str, str]:
    """
    Scrape the system summary page and return a dictionary of fields.

    On failure, raises a RuntimeError.
    """
    base_http_url = f"http://{switch_ip}/"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        try:
            try:
                page.goto(base_http_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass

            _perform_login(page, username, password)
            page.wait_for_timeout(3000)

            prefix = _detect_csb_prefix(page)

            html = _find_system_summary_html(page, switch_ip, prefix)

        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not html:
        raise RuntimeError("Unable to locate System Summary page after login.")

    data = _parse_system_summary(html)
    data["switch_ip"] = switch_ip
    return data
