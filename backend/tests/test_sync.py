import time
from datetime import datetime, timedelta, timezone


def _iso(dt):
    return dt.isoformat()


def test_pull_returns_items_changed_since(client, auth_headers):
    client.post("/notes", json={"title": "Old"}, headers=auth_headers)

    cutoff = _iso(datetime.now(timezone.utc) + timedelta(milliseconds=1))

    pull = client.get("/sync/pull", params={"since": cutoff}, headers=auth_headers)
    assert pull.status_code == 200
    assert pull.json()["notes"] == []

    pull_all = client.get("/sync/pull", headers=auth_headers)
    assert len(pull_all.json()["notes"]) == 1


def test_push_accepts_new_note(client, auth_headers):
    now = _iso(datetime.now(timezone.utc))
    push = client.post(
        "/sync/push",
        json={
            "items": [
                {
                    "item_type": "note",
                    "data": {
                        "id": "note-1",
                        "title": "Synced",
                        "content": "",
                        "tags": [],
                        "created_at": now,
                        "updated_at": now,
                    },
                }
            ]
        },
        headers=auth_headers,
    )
    assert push.status_code == 200
    result = push.json()["results"][0]
    assert result["status"] == "accepted"

    got = client.get("/notes/note-1", headers=auth_headers)
    assert got.status_code == 200


def test_push_rejects_stale_write_last_write_wins(client, auth_headers):
    now = datetime.now(timezone.utc)
    newer = _iso(now)
    older = _iso(now - timedelta(seconds=10))

    client.post(
        "/sync/push",
        json={
            "items": [
                {
                    "item_type": "note",
                    "data": {
                        "id": "note-2",
                        "title": "Newer",
                        "created_at": newer,
                        "updated_at": newer,
                    },
                }
            ]
        },
        headers=auth_headers,
    )

    push = client.post(
        "/sync/push",
        json={
            "items": [
                {
                    "item_type": "note",
                    "data": {
                        "id": "note-2",
                        "title": "Stale",
                        "created_at": older,
                        "updated_at": older,
                    },
                }
            ]
        },
        headers=auth_headers,
    )
    result = push.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["server_item"]["title"] == "Newer"


def test_pull_does_not_choke_on_attachments(client, auth_headers):
    # Attachments share GSI1 with notes/folders (same "what changed since"
    # query) but only ever have `created_at`, never `updated_at` -- pull
    # must not assume every GSI1 item has `updated_at`.
    note = client.post("/notes", json={"title": "Has an attachment"}, headers=auth_headers).json()
    client.post(
        "/attachments/presign-upload",
        json={"note_id": note["id"], "content_type": "image/png"},
        headers=auth_headers,
    )

    pull = client.get("/sync/pull", headers=auth_headers)
    assert pull.status_code == 200
    assert len(pull.json()["notes"]) == 1


def test_push_folder_tombstone_propagates(client, auth_headers):
    # Past timestamps: a future one would be capped to server now (see sync.push).
    now = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    client.post(
        "/sync/push",
        json={
            "items": [
                {
                    "item_type": "folder",
                    "data": {"id": "f1", "name": "Work", "created_at": now, "updated_at": now},
                }
            ]
        },
        headers=auth_headers,
    )

    later = _iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    client.post(
        "/sync/push",
        json={
            "items": [
                {
                    "item_type": "folder",
                    "data": {
                        "id": "f1",
                        "name": "Work",
                        "created_at": now,
                        "updated_at": later,
                        "deleted_at": later,
                    },
                }
            ]
        },
        headers=auth_headers,
    )

    pull = client.get("/sync/pull", headers=auth_headers)
    folders = pull.json()["folders"]
    assert len(folders) == 1
    assert folders[0]["deleted_at"] == later


def _push(client, auth_headers, item_type, data):
    resp = client.post(
        "/sync/push", json={"items": [{"item_type": item_type, "data": data}]}, headers=auth_headers
    )
    assert resp.status_code == 200
    return resp.json()["results"][0]


def test_push_invalid_item_is_rejected_not_500(client, auth_headers):
    # A 500 here never clears the client's sync queue, so one bad item would
    # block every later push from that device.
    result = _push(client, auth_headers, "note", {"id": "bad"})
    assert result["status"] == "rejected"


def test_pull_still_sees_item_from_slow_clock_device(client, auth_headers):
    client.post("/notes", json={"title": "first"}, headers=auth_headers)
    cursor = client.get("/sync/pull", headers=auth_headers).json()["synced_at"]

    # Another device whose clock runs an hour behind pushes a new note.
    behind = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    _push(client, auth_headers, "note", {"id": "late", "created_at": behind, "updated_at": behind})

    pull = client.get("/sync/pull", params={"since": cursor}, headers=auth_headers).json()
    assert "late" in [n["id"] for n in pull["notes"]]


def test_future_dated_push_does_not_block_later_edits(client, auth_headers):
    future = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
    _push(client, auth_headers, "note", {"id": "n", "created_at": future, "updated_at": future})

    time.sleep(0.05)  # past one Windows clock tick, so the edit is strictly newer
    edit = _iso(datetime.now(timezone.utc))
    result = _push(
        client, auth_headers, "note", {"id": "n", "title": "edit", "created_at": future, "updated_at": edit}
    )
    assert result["status"] == "accepted"
