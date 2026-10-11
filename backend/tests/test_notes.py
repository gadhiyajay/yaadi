def test_create_list_get_note(client, auth_headers):
    create = client.post(
        "/notes", json={"title": "Hello", "content": "World"}, headers=auth_headers
    )
    assert create.status_code == 201
    note = create.json()
    assert note["title"] == "Hello"

    listed = client.get("/notes", headers=auth_headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    got = client.get(f"/notes/{note['id']}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == note["id"]


def test_update_note(client, auth_headers):
    note = client.post("/notes", json={"title": "A"}, headers=auth_headers).json()
    updated = client.patch(
        f"/notes/{note['id']}", json={"title": "B"}, headers=auth_headers
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "B"
    assert updated.json()["updated_at"] >= note["updated_at"]


def test_delete_note_is_soft_and_hides_from_list(client, auth_headers):
    note = client.post("/notes", json={"title": "Temp"}, headers=auth_headers).json()
    delete = client.delete(f"/notes/{note['id']}", headers=auth_headers)
    assert delete.status_code == 204

    listed = client.get("/notes", headers=auth_headers)
    assert listed.json() == []

    got = client.get(f"/notes/{note['id']}", headers=auth_headers)
    assert got.status_code == 404


def test_get_missing_note_404(client, auth_headers):
    resp = client.get("/notes/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


def test_patch_null_title_is_rejected_and_does_not_break_list(client, auth_headers):
    # A null on a non-nullable field used to be written straight to DynamoDB,
    # after which every read of that note (and GET /notes) 500'd.
    note = client.post("/notes", json={"title": "A"}, headers=auth_headers).json()
    resp = client.patch(f"/notes/{note['id']}", json={"title": None}, headers=auth_headers)
    assert resp.status_code == 422
    assert client.get("/notes", headers=auth_headers).status_code == 200


def test_patch_null_folder_id_moves_note_to_root(client, auth_headers):
    note = client.post("/notes", json={"folder_id": "f1"}, headers=auth_headers).json()
    resp = client.patch(f"/notes/{note['id']}", json={"folder_id": None}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["folder_id"] is None
