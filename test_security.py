"""Smoke tests for v0.1.1 security fixes. Run: python test_security.py"""
import os
import tempfile

# Point at a temp config so load_config() returns an empty setup and tests can write to it.
_temp_config = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False).name
os.environ["HANGAR_CONFIG"] = _temp_config

from app import app, _safe_ipv4  # noqa: E402

client = app.test_client()


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    assert cond, name


# --- IPv4 validation ---
check("safe_ipv4 accepts 192.168.1.10", _safe_ipv4("192.168.1.10") == "192.168.1.10")
check("safe_ipv4 rejects javascript:", _safe_ipv4("javascript:alert(1)") is None)
check("safe_ipv4 rejects empty", _safe_ipv4("") is None)
check("safe_ipv4 rejects None", _safe_ipv4(None) is None)
check("safe_ipv4 rejects garbage", _safe_ipv4("<script>") is None)
check("safe_ipv4 rejects 999.999", _safe_ipv4("999.999.999.999") is None)
check("safe_ipv4 rejects IPv6", _safe_ipv4("::1") is None)

# --- CSRF guard ---
# Cross-origin POST: no HX-Request, evil Origin -> 403
r = client.post("/setup", data={"url": "x"}, headers={"Origin": "https://evil.com"})
check("cross-origin POST blocked (evil Origin)", r.status_code == 403)

# No Origin, no Referer, no HX-Request -> 403
r = client.post("/setup", data={"url": "x"})
check("bare POST (no headers) blocked", r.status_code == 403)

# HX-Request: true -> allowed through the guard (route may then 400/redirect, that's fine)
r = client.post(
    "/api/proxmox/proxmox/qemu/100/start",
    headers={"HX-Request": "true"},
)
check("HTMX POST bypasses CSRF (not 403)", r.status_code != 403)

# Same-origin Origin -> allowed
# Flask test client host is 'localhost'
r = client.post(
    "/setup",
    data={"url": ""},
    headers={"Origin": "http://localhost"},
)
check("same-origin POST allowed (not 403)", r.status_code != 403)

# Same-origin Referer -> allowed
r = client.post(
    "/setup",
    data={"url": ""},
    headers={"Referer": "http://localhost/"},
)
check("same-origin Referer allowed (not 403)", r.status_code != 403)

# GET always allowed
r = client.get("/")
check("GET / not blocked", r.status_code == 200)

# --- Credential helpers ---
from app import set_credentials, verify_credentials  # noqa: E402

set_credentials("admin", "hunter2")
check("verify_credentials accepts correct pair", verify_credentials("admin", "hunter2"))
check("verify_credentials rejects wrong password", not verify_credentials("admin", "wrong"))
check("verify_credentials rejects wrong username", not verify_credentials("root", "hunter2"))

# --- Auth guard ---
# fresh app state: credentials still set from prior test; ensure session is empty
c2 = app.test_client()

# Unauthed HTMX GET on a protected endpoint returns 401 with HX-Redirect header
r = c2.get("/api/vms", headers={"HX-Request": "true"})
check("HTMX GET blocked when unauthed", r.status_code == 401)
check("HTMX 401 carries HX-Redirect to /login", r.headers.get("HX-Redirect") == "/login")

# Unauthed browser GET of / redirects to /login
r = c2.get("/")
check("browser GET / redirects to /login when unauthed", r.status_code == 302 and r.headers["Location"].endswith("/login"))

# /login itself is reachable without auth
r = c2.get("/login")
check("/login accessible unauthed", r.status_code == 200)

# /static/* reachable without auth (favicon path stub)
r = c2.get("/static/does-not-exist.css")
check("/static/* not blocked by auth (404 not 302)", r.status_code == 404)

print("\nAll security smoke tests passed.")
