def test_presign_upload_returns_url(client, auth_headers):
    resp = client.post(
        "/attachments/presign-upload",
        json={"note_id": "note-1", "content_type": "image/png"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["upload_url"].startswith("https://")
    assert body["attachment_id"]
    assert body["s3_key"]


def test_download_url_for_missing_attachment_404s(client, auth_headers):
    resp = client.get("/attachments/does-not-exist/download-url", headers=auth_headers)
    assert resp.status_code == 404


def test_download_url_for_existing_attachment(client, auth_headers):
    created = client.post(
        "/attachments/presign-upload",
        json={"note_id": "note-1", "content_type": "image/png"},
        headers=auth_headers,
    ).json()

    resp = client.get(
        f"/attachments/{created['attachment_id']}/download-url", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["download_url"].startswith("https://")
