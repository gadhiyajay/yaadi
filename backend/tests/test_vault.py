import time

import boto3
import jwt
import pytest
from argon2 import PasswordHasher

from vault import GLOBAL_LOCKOUT_THRESHOLD, IP_LOCKOUT_THRESHOLD

PIN = "482916"
VAULT_KEY = "dGVzdC12YXVsdC1rZXktMzItYnl0ZXMtbG9uZy0hISE="


@pytest.fixture
def vault_setup(aws):
    import db

    db.put_item({"PK": db.user_pk(), "SK": db.VAULT_PIN_SK, "hash": PasswordHasher().hash(PIN), "updated_at": db.now()})
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/yaadi/vault_key", Value=VAULT_KEY, Type="SecureString"
    )


def unlock(client, auth_headers, pin=PIN, ip="203.0.113.1"):
    return client.post("/vault/unlock", json={"pin": pin}, headers={**auth_headers, "X-Client-IP": ip})


@pytest.fixture
def vault_headers(client, auth_headers, vault_setup):
    resp = unlock(client, auth_headers)
    assert resp.status_code == 200
    return {**auth_headers, "X-Vault-Token": resp.json()["vault_token"]}


def item(item_id="a1", updated_at="2026-10-23T10:00:00.000000+00:00", ct="c2VjcmV0"):
    return {"id": item_id, "ct": ct, "iv": "aXY=", "updated_at": updated_at}


# --- gate ---


def test_status_reports_setup(client, auth_headers):
    assert client.get("/vault/status", headers=auth_headers).json() == {"configured": False, "locked_until": None}


def test_unlock_before_setup_is_refused(client, auth_headers):
    assert unlock(client, auth_headers).status_code == 409


def test_unlock_returns_short_token_and_key(client, auth_headers, vault_setup):
    resp = unlock(client, auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == VAULT_KEY
    assert body["expires_in"] == 300
    assert client.get("/vault/status", headers=auth_headers).json()["configured"] is True


def test_vault_needs_the_yaadi_login_too(client, vault_setup):
    assert client.post("/vault/unlock", json={"pin": PIN}).status_code == 403


def test_wrong_pin_counts_down(client, auth_headers, vault_setup):
    resp = unlock(client, auth_headers, pin="000001")
    assert resp.status_code == 403  # not 401: the app would refresh and replay it
    assert resp.json()["detail"]["attempts_left"] == IP_LOCKOUT_THRESHOLD - 1


def test_malformed_pin_is_just_wrong(client, auth_headers, vault_setup):
    assert unlock(client, auth_headers, pin="12345").status_code == 403
    assert unlock(client, auth_headers, pin="abcdef").status_code == 403


def test_lockout_is_per_ip(client, auth_headers, vault_setup):
    for _ in range(IP_LOCKOUT_THRESHOLD):
        unlock(client, auth_headers, pin="000001", ip="198.51.100.7")
    locked = unlock(client, auth_headers, ip="198.51.100.7")
    assert locked.status_code == 429  # even the right PIN, while locked
    assert locked.json()["detail"]["locked_until"]
    assert unlock(client, auth_headers, ip="203.0.113.1").status_code == 200


def test_global_lockout_across_ips(client, auth_headers, vault_setup):
    # One failure each from many IPs: no IP is locked, but together they hit the global limit.
    for i in range(GLOBAL_LOCKOUT_THRESHOLD):
        unlock(client, auth_headers, pin="000001", ip=f"198.51.100.{i + 10}")
    assert unlock(client, auth_headers, ip="203.0.113.99").status_code == 429


def test_success_resets_the_counter(client, auth_headers, vault_setup):
    for _ in range(IP_LOCKOUT_THRESHOLD - 1):
        unlock(client, auth_headers, pin="000001")
    assert unlock(client, auth_headers).status_code == 200
    assert unlock(client, auth_headers, pin="000001").json()["detail"]["attempts_left"] == IP_LOCKOUT_THRESHOLD - 1


# --- vault token ---


def test_items_need_a_vault_token(client, auth_headers, vault_setup):
    assert client.get("/vault/items", headers=auth_headers).status_code == 403


def test_access_token_is_not_a_vault_token(client, auth_headers, vault_setup):
    access = auth_headers["Authorization"].removeprefix("Bearer ")
    resp = client.get("/vault/items", headers={**auth_headers, "X-Vault-Token": access})
    assert resp.status_code == 403


def test_vault_token_is_not_an_access_token(client, vault_headers):
    resp = client.get("/notes", headers={"Authorization": f"Bearer {vault_headers['X-Vault-Token']}"})
    assert resp.status_code == 401


def test_expired_vault_token_is_refused(client, auth_headers, vault_setup):
    past = int(time.time()) - 10
    stale = jwt.encode({"type": "vault", "iat": past - 300, "exp": past}, "test-jwt-secret", algorithm="HS256")
    assert client.get("/vault/items", headers={**auth_headers, "X-Vault-Token": stale}).status_code == 403


def test_mcp_token_does_not_open_the_vault(client, vault_setup):
    resp = client.get("/vault/items", headers={"Authorization": "Bearer some-mcp-token", "X-Vault-Token": "x"})
    assert resp.status_code == 401


# --- items ---


def test_put_and_list_items(client, vault_headers):
    assert client.put("/vault/items/a1", json=item(), headers=vault_headers).json() == {"status": "accepted", "server_item": None}
    listed = client.get("/vault/items", headers=vault_headers).json()
    assert [i["id"] for i in listed] == ["a1"]
    assert listed[0]["ct"] == "c2VjcmV0"


def test_stale_put_returns_the_server_copy(client, vault_headers):
    client.put("/vault/items/a1", json=item(updated_at="2026-10-23T11:00:00.000000+00:00", ct="bmV3"), headers=vault_headers)
    resp = client.put("/vault/items/a1", json=item(updated_at="2026-10-23T10:00:00.000000+00:00"), headers=vault_headers).json()
    assert resp["status"] == "rejected"
    assert resp["server_item"]["ct"] == "bmV3"


def test_tombstones_are_listed(client, vault_headers):
    client.put("/vault/items/a1", json={**item(), "deleted_at": "2026-10-23T10:00:00.000000+00:00"}, headers=vault_headers)
    assert client.get("/vault/items", headers=vault_headers).json()[0]["deleted_at"]


def test_item_id_must_match_url(client, vault_headers):
    assert client.put("/vault/items/other", json=item(), headers=vault_headers).status_code == 400


def test_oversized_item_is_refused(client, vault_headers):
    assert client.put("/vault/items/a1", json=item(ct="A" * 350_001), headers=vault_headers).status_code == 413


def test_vault_items_never_reach_sync_or_notes(client, auth_headers, vault_headers):
    client.put("/vault/items/a1", json=item(), headers=vault_headers)
    pulled = client.get("/sync/pull", headers=auth_headers).json()
    assert pulled["notes"] == [] and pulled["folders"] == []
    assert client.get("/notes", headers=auth_headers).json() == []


# --- PIN change + audit ---


def test_change_pin(client, auth_headers, vault_headers):
    resp = client.post("/vault/pin", json={"current_pin": PIN, "new_pin": "730518"}, headers=vault_headers)
    assert resp.status_code == 204
    assert unlock(client, auth_headers).status_code == 403
    assert unlock(client, auth_headers, pin="730518").status_code == 200


def test_change_pin_checks_the_current_one(client, vault_headers):
    resp = client.post("/vault/pin", json={"current_pin": "000001", "new_pin": "730518"}, headers=vault_headers)
    assert resp.status_code == 403


@pytest.mark.parametrize("weak", ["111111", "123456", "987654", "012345", "123123", "12345", "12a456"])
def test_change_pin_refuses_weak_or_malformed(client, vault_headers, weak):
    resp = client.post("/vault/pin", json={"current_pin": PIN, "new_pin": weak}, headers=vault_headers)
    assert resp.status_code == 422


def test_audit_lists_unlocks_newest_first(client, auth_headers, vault_headers):
    unlock(client, auth_headers, pin="000001")
    events = client.get("/vault/audit", headers=vault_headers).json()
    assert [e["event"] for e in events[:2]] == ["unlock_failed", "unlocked"]
    assert events[0]["ip"] == "203.0.113.1"


def test_is_weak_pin():
    from security import is_weak_pin

    assert is_weak_pin("000000") and is_weak_pin("456789") and is_weak_pin("543210")
    assert not is_weak_pin("482916") and not is_weak_pin("135790")
