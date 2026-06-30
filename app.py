"""Hangar - mobile control panel for Proxmox."""
import os
import re
import socket
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3
import yaml as yaml_lib
from flask import Flask, abort, jsonify, redirect, render_template, request

# Common homelab web ports in priority order. First open one wins.
WEB_PORT_CANDIDATES = (
    80, 443,
    81, 8080, 8000, 3000, 8443,
    5984, 8123, 9000, 8006, 8384,
    3001, 7878, 8989, 8181, 32400,
)
HTTPS_PORTS = {443, 8443, 8006}
PORT_PROBE_TIMEOUT = 0.35
PORT_CACHE_TTL = 300  # 5 min — ports don't change often

_port_cache = {}  # ip -> (timestamp, port_or_None)
_port_cache_lock = threading.Lock()


def _probe_port(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=PORT_PROBE_TIMEOUT):
            return port
    except (OSError, socket.timeout):
        return None


def _parse_web_tag(tags):
    """Return (port:int, https:bool) from a `web:NNNN` / `webs:NNNN` tag, or (None, False)."""
    for t in tags or ():
        m = re.match(r"^(webs?):(\d{2,5})$", t.strip().lower())
        if m:
            port = int(m.group(2))
            if 1 <= port <= 65535:
                return port, m.group(1) == "webs"
    return None, False


def detect_web_port(ip, tags):
    """Return (port, scheme) for the given IP. Tag override wins, else parallel TCP probe with cache."""
    if not ip:
        return None, "http"

    tag_port, tag_https = _parse_web_tag(tags)
    if tag_port is not None:
        scheme = "https" if tag_https or tag_port in HTTPS_PORTS else "http"
        return tag_port, scheme

    now = time.time()
    with _port_cache_lock:
        cached = _port_cache.get(ip)
        if cached and now - cached[0] < PORT_CACHE_TTL:
            port = cached[1]
            scheme = "https" if port in HTTPS_PORTS else "http"
            return port, scheme

    found = None
    with ThreadPoolExecutor(max_workers=min(10, len(WEB_PORT_CANDIDATES))) as ex:
        futs = {ex.submit(_probe_port, ip, p): p for p in WEB_PORT_CANDIDATES}
        results = {}
        for fut, p in futs.items():
            try:
                if fut.result():
                    results[p] = True
            except Exception:
                pass
        for p in WEB_PORT_CANDIDATES:
            if results.get(p):
                found = p
                break

    with _port_cache_lock:
        _port_cache[ip] = (now, found)
    scheme = "https" if found in HTTPS_PORTS else "http"
    return found, scheme

CONFIG_PATH = os.environ.get("HANGAR_CONFIG", "hangar.yaml")
DEFAULT_ACCENT = "#3b82f6"
CACHE_TTL = 3.0
REQ_TIMEOUT = 4
ACTION_TIMEOUT = 8
POLL_STRIP_S = 4
POLL_VMS_S = 5

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _accent_fg(hex_color):
    """Pick black or white text for a given background color (WCAG-ish luminance)."""
    h = (hex_color or "#3b82f6").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "#ffffff"
    def chan(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    L = 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)
    return "#0a0a0a" if L > 0.5 else "#ffffff"


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = yaml_lib.safe_load(f) or {}

    nodes = cfg.get("nodes") or []

    if not nodes and os.environ.get("HANGAR_NODE_URL"):
        nodes = [{
            "name": os.environ.get("HANGAR_NODE_NAME", "proxmox"),
            "url": os.environ["HANGAR_NODE_URL"],
            "token_id": os.environ.get("HANGAR_NODE_TOKEN_ID", ""),
            "token_secret": os.environ.get("HANGAR_NODE_TOKEN", ""),
            "verify_ssl": _truthy(os.environ.get("HANGAR_NODE_VERIFY_SSL", "false")),
        }]

    valid_nodes = [
        n for n in nodes
        if n.get("url") and n.get("token_id") and n.get("token_secret")
    ]

    theme = cfg.get("theme") or {}
    accent = (
        os.environ.get("HANGAR_ACCENT")
        or theme.get("accent")
        or DEFAULT_ACCENT
    )
    scheme = (theme.get("scheme") or "auto").lower()
    if scheme not in ("auto", "light", "dark"):
        scheme = "auto"

    settings = cfg.get("settings") or {}
    try:
        hold_ms = max(300, min(3000, int(settings.get("hold_ms") or 1000)))
    except (TypeError, ValueError):
        hold_ms = 1000

    telegram = cfg.get("telegram") or {}
    if not isinstance(telegram, dict):
        telegram = {}
    telegram.setdefault("events", {})
    for ev in ("started", "stopped", "restarted", "failed"):
        telegram["events"].setdefault(ev, False)

    cards = cfg.get("cards") or {}
    if not isinstance(cards, dict):
        cards = {}

    return {
        "nodes": valid_nodes,
        "accent": accent,
        "scheme": scheme,
        "hold_ms": hold_ms,
        "telegram": telegram,
        "cards": cards,
    }


CONFIG = load_config()
NODES = CONFIG["nodes"]
ACCENT = CONFIG["accent"]
ACCENT_FG = _accent_fg(ACCENT)
THEME_SCHEME = CONFIG["scheme"]
HOLD_MS_CFG = CONFIG["hold_ms"]
TELEGRAM = CONFIG["telegram"]
CARDS = CONFIG["cards"]

CARD_DEFAULTS = {
    "alias": None,
    "ip": None,
    "web_port": None,
    "show_console": True,
    "show_ip": True,
    "show_meta": True,
    "show_stats": True,
}


def _card_key(pve_node, kind, vmid):
    return f"{pve_node}/{kind}/{vmid}"


def _card_settings(pve_node, kind, vmid):
    """Resolve a card's settings: defaults overlaid with stored overrides."""
    merged = dict(CARD_DEFAULTS)
    stored = CARDS.get(_card_key(pve_node, kind, vmid)) or {}
    for k in merged:
        if k in stored:
            merged[k] = stored[k]
    return merged


def save_config():
    """Write the current in-memory config to YAML, preserving secrets."""
    cfg = {
        "theme": {"accent": ACCENT, "scheme": THEME_SCHEME},
        "settings": {"hold_ms": HOLD_MS_CFG},
        "telegram": TELEGRAM,
        "nodes": NODES,
        "cards": CARDS,
    }
    with open(CONFIG_PATH, "w") as f:
        yaml_lib.dump(cfg, f, default_flow_style=False, sort_keys=False)


def reload_globals():
    global CONFIG, NODES, ACCENT, ACCENT_FG, THEME_SCHEME, HOLD_MS_CFG, TELEGRAM
    CONFIG = load_config()
    NODES = CONFIG["nodes"]
    ACCENT = CONFIG["accent"]
    ACCENT_FG = _accent_fg(ACCENT)
    THEME_SCHEME = CONFIG["scheme"]
    HOLD_MS_CFG = CONFIG["hold_ms"]
    TELEGRAM = CONFIG["telegram"]
    _invalidate()


def telegram_notify(text):
    """Send a Telegram message synchronously. Returns True on success."""
    bot = (TELEGRAM or {}).get("bot_token", "").strip()
    chat = (TELEGRAM or {}).get("chat_id", "").strip()
    if not (bot and chat):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={
                "chat_id": chat,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def telegram_notify_async(text):
    threading.Thread(target=telegram_notify, args=(text,), daemon=True).start()


_cache = {"data": None, "t": 0.0}
_lock = threading.Lock()
_history = deque(maxlen=12)
_history_lock = threading.Lock()


def add_history(action, vm_name, kind, vmid, ok):
    with _history_lock:
        _history.append({
            "t": time.time(),
            "action": action,
            "vm": vm_name,
            "kind": kind,
            "vmid": vmid,
            "ok": ok,
        })


def _auth(n):
    return {"Authorization": f"PVEAPIToken={n['token_id']}={n['token_secret']}"}


def _node_cfg(name):
    for n in NODES:
        if n["name"] == name:
            return n
    abort(404, f"unknown node {name}")


def _fetch_one(n):
    r = requests.get(
        f"{n['url']}/api2/json/cluster/resources",
        headers=_auth(n),
        verify=n.get("verify_ssl", False),
        timeout=REQ_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["data"]


def _get_vm_extras(n, pve_node, kind, vmid, status):
    """Fetch tags + IP for one VM. Best-effort: failures return defaults."""
    out = {"tags": [], "ip": None}
    try:
        r = requests.get(
            f"{n['url']}/api2/json/nodes/{pve_node}/{kind}/{vmid}/config",
            headers=_auth(n),
            verify=n.get("verify_ssl", False),
            timeout=REQ_TIMEOUT,
        )
        r.raise_for_status()
        cfg = r.json().get("data") or {}
        raw_tags = cfg.get("tags", "")
        if raw_tags:
            out["tags"] = [t.strip() for t in re.split(r"[;,]", raw_tags) if t.strip()]
        if kind == "lxc":
            net0 = cfg.get("net0", "")
            m = re.search(r"ip=([0-9.]+)/", net0)
            if m:
                out["ip"] = m.group(1)
    except Exception:
        pass

    if kind == "lxc" and status == "running" and not out["ip"]:
        try:
            r = requests.get(
                f"{n['url']}/api2/json/nodes/{pve_node}/lxc/{vmid}/interfaces",
                headers=_auth(n),
                verify=n.get("verify_ssl", False),
                timeout=REQ_TIMEOUT,
            )
            r.raise_for_status()
            for iface in (r.json().get("data") or []):
                if iface.get("name") == "lo":
                    continue
                inet = iface.get("inet") or ""
                if inet and not inet.startswith("127."):
                    out["ip"] = inet.split("/")[0]
                    break
        except Exception:
            pass

    if kind == "qemu" and status == "running" and not out["ip"]:
        try:
            r = requests.get(
                f"{n['url']}/api2/json/nodes/{pve_node}/qemu/{vmid}/agent/network-get-interfaces",
                headers=_auth(n),
                verify=n.get("verify_ssl", False),
                timeout=REQ_TIMEOUT,
            )
            if r.status_code == 200:
                ifaces = ((r.json().get("data") or {}).get("result") or [])
                for iface in ifaces:
                    name = iface.get("name", "")
                    if name in ("lo", "Loopback Pseudo-Interface 1") or name.startswith("docker"):
                        continue
                    for addr in iface.get("ip-addresses", []):
                        ip = addr.get("ip-address", "")
                        if addr.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
                            out["ip"] = ip
                            return out
        except Exception:
            pass

    return out


def fetch_all():
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["t"] < CACHE_TTL:
            return _cache["data"]

    out = {"nodes": [], "vms": [], "errors": [], "running": 0, "stopped": 0}
    with ThreadPoolExecutor(max_workers=max(1, len(NODES))) as ex:
        futures = {ex.submit(_fetch_one, n): n for n in NODES}
        for fut, n in futures.items():
            try:
                resources = fut.result()
            except Exception as e:
                out["nodes"].append({
                    "name": n["name"], "status": "offline",
                    "cpu_pct": 0, "mem_pct": 0,
                })
                out["errors"].append({"node": n["name"], "error": str(e)[:120]})
                continue
            for r in resources:
                if r.get("type") == "node":
                    maxmem = r.get("maxmem") or 1
                    out["nodes"].append({
                        "name": r.get("node") or n["name"],
                        "status": r.get("status", "unknown"),
                        "cpu_pct": round((r.get("cpu") or 0) * 100),
                        "mem_pct": round((r.get("mem") or 0) / maxmem * 100),
                    })
                elif r.get("type") in ("qemu", "lxc"):
                    maxmem = r.get("maxmem") or 1
                    maxdisk = r.get("maxdisk") or 1
                    disk_used = r.get("disk") or 0
                    status = r.get("status", "unknown")
                    kind = r["type"]
                    pve_node = r.get("node")
                    vmid = r["vmid"]
                    console_kind = "qemu" if kind == "qemu" else "lxc"
                    out["vms"].append({
                        "hangar_node": n["name"],
                        "pve_node": pve_node,
                        "kind": kind,
                        "vmid": vmid,
                        "name": r.get("name") or f"#{vmid}",
                        "status": status,
                        "cpu_pct": round((r.get("cpu") or 0) * 100),
                        "mem_pct": round((r.get("mem") or 0) / maxmem * 100),
                        "disk_pct": round(disk_used / maxdisk * 100) if disk_used else 0,
                        "disk_known": disk_used > 0,
                        "uptime_s": r.get("uptime") or 0,
                        "_n": n,
                        "tags": [],
                        "ip": None,
                        "console_url": f"{n['url']}/#v1:0:={console_kind}%2F{vmid}:4",
                    })
                    if status == "running":
                        out["running"] += 1
                    elif status == "stopped":
                        out["stopped"] += 1

    if out["vms"]:
        with ThreadPoolExecutor(max_workers=min(20, len(out["vms"]))) as ex:
            futs = {
                ex.submit(_get_vm_extras, vm["_n"], vm["pve_node"], vm["kind"], vm["vmid"], vm["status"]): vm
                for vm in out["vms"]
            }
            for fut, vm in futs.items():
                try:
                    extra = fut.result(timeout=REQ_TIMEOUT)
                    vm["tags"] = extra["tags"]
                    vm["ip"] = extra["ip"]
                except Exception:
                    pass

        for vm in out["vms"]:
            cs = _card_settings(vm["pve_node"], vm["kind"], vm["vmid"])
            vm["alias"] = cs["alias"]
            vm["display_name"] = cs["alias"] or vm["name"]
            vm["show_console"] = cs["show_console"]
            vm["show_ip"] = cs["show_ip"]
            vm["show_meta"] = cs["show_meta"]
            vm["show_stats"] = cs["show_stats"]
            if cs["ip"]:
                vm["ip"] = cs["ip"]
            vm["_web_port_override"] = cs["web_port"]

        targets = [vm for vm in out["vms"] if vm["status"] == "running" and vm["ip"] and vm["show_ip"]]
        if targets:
            with ThreadPoolExecutor(max_workers=min(20, len(targets))) as ex:
                futs = {}
                for vm in targets:
                    if vm["_web_port_override"]:
                        port = int(vm["_web_port_override"])
                        scheme = "https" if port in HTTPS_PORTS else "http"
                        vm["web_port"] = port
                        vm["web_url"] = f"{scheme}://{vm['ip']}:{port}"
                    else:
                        futs[ex.submit(detect_web_port, vm["ip"], vm["tags"])] = vm
                for fut, vm in futs.items():
                    try:
                        port, scheme = fut.result(timeout=PORT_PROBE_TIMEOUT * len(WEB_PORT_CANDIDATES) + 1)
                    except Exception:
                        port, scheme = None, "http"
                    vm["web_port"] = port
                    vm["web_url"] = f"{scheme}://{vm['ip']}:{port}" if port else None
        for vm in out["vms"]:
            vm.setdefault("web_port", None)
            vm.setdefault("web_url", None)
            vm.pop("_n", None)
            vm.pop("_web_port_override", None)

    out["vms"].sort(key=lambda v: (v["pve_node"], v["vmid"]))
    out["nodes"].sort(key=lambda n: n["name"])
    with _lock:
        _cache["data"] = out
        _cache["t"] = time.time()
    return out


def _invalidate():
    with _lock:
        _cache["t"] = 0.0


def _act(hangar_node, pve_node, kind, vmid, action):
    n = _node_cfg(hangar_node)
    r = requests.post(
        f"{n['url']}/api2/json/nodes/{pve_node}/{kind}/{vmid}/status/{action}",
        headers=_auth(n),
        verify=n.get("verify_ssl", False),
        timeout=ACTION_TIMEOUT,
    )
    r.raise_for_status()
    _invalidate()


PENDING_FOR = {"start": "starting", "shutdown": "stopping", "reboot": "restarting"}
EVENT_FOR = {"start": "started", "shutdown": "stopped", "reboot": "restarted"}
EMOJI_FOR = {"started": "▶️", "stopped": "🛑", "restarted": "🔄"}


@app.template_filter("uptime")
def _fmt_uptime(s):
    s = int(s or 0)
    if s <= 0:
        return "—"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


@app.template_filter("ago")
def _fmt_ago(t):
    if not t:
        return "—"
    s = int(time.time() - t)
    if s < 5:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


@app.context_processor
def _inject():
    return {
        "POLL_STRIP_S": POLL_STRIP_S,
        "POLL_VMS_S": POLL_VMS_S,
        "HOLD_MS": HOLD_MS_CFG,
        "accent": ACCENT,
        "accent_fg": ACCENT_FG,
        "scheme": THEME_SCHEME,
    }


def _setup_required():
    return not NODES


@app.route("/")
def index():
    if _setup_required():
        return render_template("setup.html")
    return render_template("index.html", data=fetch_all())


@app.route("/setup", methods=["POST"])
def setup_submit():
    if not _setup_required():
        return redirect("/")
    url = (request.form.get("url") or "").strip().rstrip("/")
    token_id = (request.form.get("token_id") or "").strip()
    token_secret = (request.form.get("token_secret") or "").strip()
    name = (request.form.get("name") or "proxmox").strip()
    verify_ssl = "verify_ssl" in request.form

    form_state = {"url": url, "token_id": token_id, "name": name, "verify_ssl": verify_ssl}

    if not (url and token_id and token_secret):
        return render_template("setup.html", error="All fields are required.", form=form_state)

    try:
        r = requests.get(
            f"{url}/api2/json/cluster/resources",
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
            verify=verify_ssl,
            timeout=8,
        )
        r.raise_for_status()
    except Exception as e:
        return render_template(
            "setup.html",
            error=f"Couldn't connect: {str(e)[:200]}",
            form=form_state,
        )

    new_cfg = {
        "theme": {"accent": ACCENT, "scheme": THEME_SCHEME},
        "settings": {"hold_ms": HOLD_MS_CFG},
        "telegram": TELEGRAM,
        "nodes": [{
            "name": name, "url": url,
            "token_id": token_id, "token_secret": token_secret,
            "verify_ssl": verify_ssl,
        }],
    }
    try:
        with open(CONFIG_PATH, "w") as f:
            yaml_lib.dump(new_cfg, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        return render_template(
            "setup.html",
            error=f"Connected, but couldn't save config to {CONFIG_PATH}: {e}.",
            form=form_state,
        )

    reload_globals()
    return redirect("/")


@app.route("/settings", methods=["GET"])
def settings_page():
    return render_template(
        "settings.html",
        cfg_accent=ACCENT,
        cfg_scheme=THEME_SCHEME,
        cfg_hold_ms=HOLD_MS_CFG,
        cfg_telegram=TELEGRAM,
        saved=request.args.get("saved") == "1",
    )


@app.route("/settings", methods=["POST"])
def settings_save():
    global ACCENT, ACCENT_FG, THEME_SCHEME, HOLD_MS_CFG, TELEGRAM

    new_accent = (request.form.get("accent") or DEFAULT_ACCENT).strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", new_accent):
        new_accent = DEFAULT_ACCENT
    ACCENT = new_accent
    ACCENT_FG = _accent_fg(ACCENT)

    s = (request.form.get("scheme") or "auto").lower()
    THEME_SCHEME = s if s in ("auto", "light", "dark") else "auto"

    try:
        HOLD_MS_CFG = max(300, min(3000, int(request.form.get("hold_ms") or 1000)))
    except ValueError:
        HOLD_MS_CFG = 1000

    TELEGRAM = {
        "bot_token": (request.form.get("tg_bot_token") or "").strip(),
        "chat_id": (request.form.get("tg_chat_id") or "").strip(),
        "events": {
            "started":   "tg_started"   in request.form,
            "stopped":   "tg_stopped"   in request.form,
            "restarted": "tg_restarted" in request.form,
            "failed":    "tg_failed"    in request.form,
        },
    }

    try:
        save_config()
    except Exception as e:
        return render_template(
            "settings.html",
            cfg_accent=ACCENT, cfg_scheme=THEME_SCHEME,
            cfg_hold_ms=HOLD_MS_CFG, cfg_telegram=TELEGRAM,
            error=f"Couldn't save config: {e}",
        )
    return redirect("/settings?saved=1")


@app.route("/settings/telegram/test", methods=["POST"])
def settings_telegram_test():
    if not ((TELEGRAM or {}).get("bot_token") and (TELEGRAM or {}).get("chat_id")):
        return jsonify(ok=False, error="Save bot token and chat ID first."), 400
    ok = telegram_notify("🛰️ Hangar test notification. Your bot is configured ✓")
    return jsonify(ok=ok, error=None if ok else "Telegram rejected the request.")


@app.route("/api/strip")
def strip():
    return render_template("_node_strip.html", data=fetch_all())


@app.route("/api/vms")
def vms():
    return render_template("_vm_list.html", data=fetch_all())


@app.route("/api/history")
def history():
    with _history_lock:
        entries = list(_history)
    entries.reverse()
    return render_template("_history.html", entries=entries)


@app.route("/api/<hangar_node>/<pve_node>/<kind>/<int:vmid>/tags", methods=["POST"])
def set_tags(hangar_node, pve_node, kind, vmid):
    if kind not in {"qemu", "lxc"}:
        abort(400, "bad kind")
    raw = (request.form.get("tags") or "").strip()
    parts = [t.strip().lower() for t in re.split(r"[;,\s]+", raw) if t.strip()]
    cleaned = []
    for t in parts:
        if re.fullmatch(r"[a-z0-9][a-z0-9_\-:.]{0,63}", t) and t not in cleaned:
            cleaned.append(t)
    tags_value = ";".join(cleaned)

    n = _node_cfg(hangar_node)
    try:
        r = requests.put(
            f"{n['url']}/api2/json/nodes/{pve_node}/{kind}/{vmid}/config",
            headers=_auth(n),
            data={"tags": tags_value},
            verify=n.get("verify_ssl", False),
            timeout=ACTION_TIMEOUT,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        body = (e.response.text or "")[:200] if e.response is not None else ""
        return f'<div class="errors"><div class="e">tag update failed: {e} {body}</div></div>', 502
    except Exception as e:
        return f'<div class="errors"><div class="e">tag update failed: {e}</div></div>', 502
    _invalidate()
    return render_template("_vm_list.html", data=fetch_all())


@app.route("/api/card/<hangar_node>/<pve_node>/<kind>/<int:vmid>", methods=["GET"])
def card_get(hangar_node, pve_node, kind, vmid):
    if kind not in {"qemu", "lxc"}:
        abort(400, "bad kind")
    return jsonify(_card_settings(pve_node, kind, vmid))


@app.route("/api/card/<hangar_node>/<pve_node>/<kind>/<int:vmid>", methods=["POST"])
def card_save(hangar_node, pve_node, kind, vmid):
    if kind not in {"qemu", "lxc"}:
        abort(400, "bad kind")
    key = _card_key(pve_node, kind, vmid)

    alias = (request.form.get("alias") or "").strip()[:64] or None

    ip_raw = (request.form.get("ip") or "").strip()
    ip_override = ip_raw if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip_raw) else None

    port_raw = (request.form.get("web_port") or "").strip()
    if port_raw:
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                port = None
        except ValueError:
            port = None
    else:
        port = None

    def flag(name):
        return request.form.get(name, "0") in ("1", "true", "on")

    overrides = {
        "alias": alias,
        "ip": ip_override,
        "web_port": port,
        "show_console": flag("show_console"),
        "show_ip": flag("show_ip"),
        "show_meta": flag("show_meta"),
        "show_stats": flag("show_stats"),
    }
    pruned = {k: v for k, v in overrides.items() if v != CARD_DEFAULTS[k]}
    if pruned:
        CARDS[key] = pruned
    else:
        CARDS.pop(key, None)

    try:
        save_config()
    except Exception as e:
        return f'<div class="errors"><div class="e">save failed: {e}</div></div>', 500

    if port is not None:
        ip_for_vm = None
        for vm in (fetch_all().get("vms") or []):
            if vm["pve_node"] == pve_node and vm["kind"] == kind and vm["vmid"] == vmid:
                ip_for_vm = vm.get("ip")
                break
        if ip_for_vm:
            with _port_cache_lock:
                _port_cache.pop(ip_for_vm, None)
    _invalidate()
    return render_template("_vm_list.html", data=fetch_all())


@app.route("/api/<hangar_node>/<pve_node>/<kind>/<int:vmid>/<action>", methods=["POST"])
def action(hangar_node, pve_node, kind, vmid, action):
    if action not in {"start", "shutdown", "reboot"}:
        abort(400, "bad action")
    if kind not in {"qemu", "lxc"}:
        abort(400, "bad kind")

    pre_data = fetch_all()
    vm_name = next(
        (vm["name"] for vm in pre_data["vms"]
         if vm["pve_node"] == pve_node and vm["vmid"] == vmid),
        str(vmid),
    )
    events = (TELEGRAM or {}).get("events") or {}

    try:
        _act(hangar_node, pve_node, kind, vmid, action)
    except requests.HTTPError as e:
        body = (e.response.text or "")[:200] if e.response is not None else ""
        if events.get("failed"):
            telegram_notify_async(f"❌ *{vm_name}*: {action} failed: {str(e)[:120]} {body[:80]}")
        add_history(action, vm_name, kind, vmid, False)
        return f'<div class="errors"><div class="e">action failed: {e} {body}</div></div>', 502
    except Exception as e:
        if events.get("failed"):
            telegram_notify_async(f"❌ *{vm_name}*: {action} failed: {str(e)[:120]}")
        add_history(action, vm_name, kind, vmid, False)
        return f'<div class="errors"><div class="e">action failed: {e}</div></div>', 502

    add_history(action, vm_name, kind, vmid, True)
    ev = EVENT_FOR.get(action)
    if ev and events.get(ev):
        telegram_notify_async(f"{EMOJI_FOR.get(ev, '•')} *{vm_name}*: {ev}")

    data = fetch_all()
    for vm in data["vms"]:
        if vm["pve_node"] == pve_node and vm["vmid"] == vmid:
            vm["status"] = PENDING_FOR.get(action, vm["status"])
            vm["pending"] = True
            break
    return render_template("_vm_list.html", data=data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
