from .conftest import TEST_PASSWORD

from auth import LOGIN_LOCKOUT_THRESHOLD


def test_login_success(client):
    resp = client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]


def test_login_wrong_password(client):
    resp = client.post("/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


def test_refresh(client):
    login = client.post("/auth/login", json={"password": TEST_PASSWORD})
    refresh_token = login.json()["refresh_token"]
    resp = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_refresh_rejects_access_token(client):
    login = client.post("/auth/login", json={"password": TEST_PASSWORD})
    access_token = login.json()["access_token"]
    resp = client.post("/auth/refresh", json={"refresh_token": access_token})
    assert resp.status_code == 401


def test_notes_requires_auth(client):
    resp = client.get("/notes")
    assert resp.status_code == 403  # no bearer credentials supplied at all


def test_login_locks_out_after_repeated_failures(client):
    for _ in range(LOGIN_LOCKOUT_THRESHOLD):
        resp = client.post("/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401

    locked = client.post("/auth/login", json={"password": "wrong"})
    assert locked.status_code == 429

    # Even the correct password is rejected while locked out.
    still_locked = client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert still_locked.status_code == 429


def test_successful_login_resets_the_failure_counter(client):
    for _ in range(LOGIN_LOCKOUT_THRESHOLD - 1):
        resp = client.post("/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401

    ok = client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert ok.status_code == 200

    # Counter was reset by the successful login, so this single failure
    # shouldn't trip the lockout.
    resp = client.post("/auth/login", json={"password": "wrong"})
    assert resp.status_code == 401


def test_lockout_is_per_client_ip(client):
    attacker = {"X-Client-IP": "203.0.113.9"}
    for _ in range(LOGIN_LOCKOUT_THRESHOLD):
        assert client.post("/auth/login", json={"password": "wrong"}, headers=attacker).status_code == 401
    assert client.post("/auth/login", json={"password": TEST_PASSWORD}, headers=attacker).status_code == 429

    # The owner, on another IP, isn't locked out by someone else's failures.
    owner = {"X-Client-IP": "198.51.100.7"}
    assert client.post("/auth/login", json={"password": TEST_PASSWORD}, headers=owner).status_code == 200
