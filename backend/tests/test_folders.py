def test_create_list_get_folder(client, auth_headers):
    create = client.post("/folders", json={"name": "Work"}, headers=auth_headers)
    assert create.status_code == 201
    folder = create.json()
    assert folder["name"] == "Work"

    listed = client.get("/folders", headers=auth_headers)
    assert len(listed.json()) == 1

    got = client.get(f"/folders/{folder['id']}", headers=auth_headers)
    assert got.status_code == 200


def test_update_folder(client, auth_headers):
    folder = client.post("/folders", json={"name": "A"}, headers=auth_headers).json()
    updated = client.patch(
        f"/folders/{folder['id']}", json={"name": "B"}, headers=auth_headers
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "B"


def test_delete_folder_is_soft_and_hides_from_list(client, auth_headers):
    folder = client.post("/folders", json={"name": "Temp"}, headers=auth_headers).json()
    delete = client.delete(f"/folders/{folder['id']}", headers=auth_headers)
    assert delete.status_code == 204

    listed = client.get("/folders", headers=auth_headers)
    assert listed.json() == []


def test_get_missing_folder_404(client, auth_headers):
    resp = client.get("/folders/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404
