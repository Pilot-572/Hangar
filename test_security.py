"""Smoke tests for v0.1.1 security fixes. Run: python test_security.py"""
import os

# Point at a nonexistent config so load_config() returns an empty setup.
os.environ["HANGAR_CONFIG"] = "/nonexistent-hangar-test.yaml"

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

print("\nAll security smoke tests passed.")
