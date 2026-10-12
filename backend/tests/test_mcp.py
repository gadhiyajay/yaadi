import hashlib

import boto3
import pytest

MCP_TOKEN = "test-mcp-token"


@pytest.fixture
def mcp(client, aws):
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/yaadi/mcp_token_hash",
        Value=hashlib.sha256(MCP_TOKEN.encode()).hexdigest(),
        Type="SecureString",
    )
    headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
    ids = iter(range(1, 10_000))

    def rpc(method, params=None):
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params or {}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def call(tool, /, **arguments):
        return rpc("tools/call", {"name": tool, "arguments": arguments})["result"]

    rpc.call = call
    rpc.headers = headers
    return rpc


def ok(result):
    assert result["isError"] is False, result["content"][0]["text"]
    return result["structuredContent"]


def error_text(result):
    assert result["isError"] is True
    return result["content"][0]["text"]


# --- protocol ---


def test_initialize_negotiates_version(mcp):
    result = mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "yaadi"

    result = mcp("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == "2025-11-25"


def test_notifications_get_202(client, mcp):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=mcp.headers)
    assert resp.status_code == 202
    assert resp.content == b""


def test_ping_and_unknown_method(mcp):
    assert mcp("ping")["result"] == {}
    assert mcp("resources/list")["error"]["code"] == -32601


def test_bad_requests(client, mcp):
    assert client.post("/mcp", content=b"{nope", headers=mcp.headers).json()["error"]["code"] == -32700
    assert client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=mcp.headers).status_code == 400
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**mcp.headers, "MCP-Protocol-Version": "1999-01-01"},
    )
    assert resp.status_code == 400


def test_get_and_delete_are_405(client, mcp):
    assert client.get("/mcp", headers=mcp.headers).status_code == 405
    assert client.delete("/mcp", headers=mcp.headers).status_code == 405


def test_tools_list(mcp):
    tools = {t["name"]: t for t in mcp("tools/list")["result"]["tools"]}
    assert set(tools) == {
        "search_notes", "list_notes", "get_note", "create_note", "update_note",
        "append_to_note", "delete_note", "list_folders", "create_folder", "list_tags",
    }
    assert tools["get_note"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_note"]["annotations"]["destructiveHint"] is True
    assert tools["create_note"]["annotations"]["destructiveHint"] is False
    assert tools["create_note"]["inputSchema"]["required"] == ["title"]
    assert tools["create_note"]["description"]


def test_unknown_tool_and_bad_arguments(mcp):
    assert mcp("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602
    assert "title" in error_text(mcp.call("create_note"))


# --- auth ---


def test_requires_mcp_token(client, mcp, auth_headers):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    resp = client.post("/mcp", json=body)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert client.post("/mcp", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    # The PWA's own access token doesn't open /mcp...
    assert client.post("/mcp", json=body, headers=auth_headers).status_code == 401
    # ...and the MCP token doesn't open the REST API.
    assert client.get("/notes", headers=mcp.headers).status_code == 401


def test_no_token_configured_means_mcp_is_off(client, aws):
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer anything"}
    )
    assert resp.status_code == 401


# --- tools ---


def test_create_get_and_list_note(mcp):
    created = ok(mcp.call("create_note", title="Groceries", content="- milk", tags=["home"]))
    assert created["folder"] is None

    got = ok(mcp.call("get_note", note_id=created["id"]))
    assert got["content"] == "- milk"
    assert got["updated_at"] == created["updated_at"]

    listed = ok(mcp.call("list_notes"))
    assert listed["total"] == 1
    assert listed["notes"][0]["preview"] == "- milk"


def test_get_missing_note(mcp):
    assert "No note" in error_text(mcp.call("get_note", note_id="nope"))


def test_folders_by_name_and_path(mcp):
    work = ok(mcp.call("create_folder", name="Work"))
    aws = ok(mcp.call("create_folder", name="AWS", parent="Work"))
    assert aws["path"] == "Work/AWS"

    note = ok(mcp.call("create_note", title="Bill", folder="Work/AWS"))
    assert note["folder"] == "Work/AWS"
    ok(mcp.call("create_note", title="Elsewhere"))

    # A folder filter includes subfolders.
    assert [n["title"] for n in ok(mcp.call("list_notes", folder="Work"))["notes"]] == ["Bill"]
    assert [n["title"] for n in ok(mcp.call("list_notes", folder="/"))["notes"]] == ["Elsewhere"]

    folders = ok(mcp.call("list_folders"))
    assert [(f["path"], f["notes"]) for f in folders["folders"]] == [("Work", 0), ("Work/AWS", 1)]
    assert folders["top_level_notes"] == 1
    assert ok(mcp.call("list_notes", folder=work["id"]))["total"] == 1


def test_ambiguous_and_missing_folder(mcp):
    for parent in ("Work", "Home"):
        ok(mcp.call("create_folder", name=parent))
        ok(mcp.call("create_folder", name="Ideas", parent=parent))
    assert "several folders" in error_text(mcp.call("create_note", title="x", folder="ideas"))
    assert ok(mcp.call("create_note", title="x", folder="Work/Ideas"))["folder"] == "Work/Ideas"

    # A full path wins over a bare-name match deeper down.
    ok(mcp.call("create_folder", name="Ideas"))
    assert ok(mcp.call("create_note", title="x", folder="Ideas"))["folder"] == "Ideas"
    assert "No folder" in error_text(mcp.call("create_note", title="x", folder="Nope"))


def test_search_ranks_title_hits_first(mcp):
    ok(mcp.call("create_note", title="Shopping", content="buy an aws gift card"))
    ok(mcp.call("create_note", title="AWS bill", content="was too high in march"))
    ok(mcp.call("create_note", title="Unrelated", content="nothing here"))

    found = ok(mcp.call("search_notes", query="aws"))
    assert found["total_matches"] == 2
    assert [n["title"] for n in found["notes"]] == ["AWS bill", "Shopping"]
    assert "aws gift card" in found["notes"][1]["preview"]

    # Every word has to match.
    assert ok(mcp.call("search_notes", query="aws march"))["total_matches"] == 1


def test_update_note_and_stale_check(mcp):
    note = ok(mcp.call("create_note", title="Draft", content="v1"))
    ok(mcp.call("create_folder", name="Done"))

    updated = ok(mcp.call("update_note", note_id=note["id"], expected_updated_at=note["updated_at"], content="v2", folder="Done"))
    assert updated["content"] == "v2"
    assert updated["title"] == "Draft"
    assert updated["folder"] == "Done"

    # A second edit based on the first read is refused, and writes nothing.
    stale = mcp.call("update_note", note_id=note["id"], expected_updated_at=note["updated_at"], content="v3")
    assert "changed since you read it" in error_text(stale)
    assert ok(mcp.call("get_note", note_id=note["id"]))["content"] == "v2"

    moved = ok(mcp.call("update_note", note_id=note["id"], folder="/"))
    assert moved["folder"] is None
    assert "Nothing to change" in error_text(mcp.call("update_note", note_id=note["id"]))


def test_append_keeps_existing_content(mcp):
    note = ok(mcp.call("create_note", title="List", content="- milk\n"))
    ok(mcp.call("append_to_note", note_id=note["id"], text="- eggs"))
    assert ok(mcp.call("get_note", note_id=note["id"]))["content"] == "- milk\n- eggs"

    empty = ok(mcp.call("create_note", title="Empty"))
    ok(mcp.call("append_to_note", note_id=empty["id"], text="first"))
    assert ok(mcp.call("get_note", note_id=empty["id"]))["content"] == "first"


def test_content_size_is_capped(mcp):
    assert "Invalid arguments" in error_text(mcp.call("create_note", title="big", content="x" * 100_001))


def test_delete_note_is_soft(client, mcp, auth_headers):
    note = ok(mcp.call("create_note", title="Temp"))
    assert ok(mcp.call("delete_note", note_id=note["id"]))["deleted"] == note["id"]
    assert ok(mcp.call("list_notes"))["total"] == 0
    assert "No note" in error_text(mcp.call("delete_note", note_id=note["id"]))

    # Still there as a tombstone, so other devices learn about the delete.
    pulled = client.get("/sync/pull", headers=auth_headers).json()
    assert pulled["notes"][0]["deleted_at"]


def test_list_tags(mcp):
    ok(mcp.call("create_note", title="a", tags=["work", "aws"]))
    ok(mcp.call("create_note", title="b", tags=["work"]))
    assert ok(mcp.call("list_tags"))["tags"] == [{"tag": "work", "notes": 2}, {"tag": "aws", "notes": 1}]
    assert [n["title"] for n in ok(mcp.call("list_notes", tag="#aws"))["notes"]] == ["a"]


def test_private_notes_are_invisible(client, mcp, auth_headers):
    secret = client.post(
        "/notes", json={"title": "Bank", "content": "pin 1234", "tags": ["Private"]}, headers=auth_headers
    ).json()
    ok(mcp.call("create_note", title="Public", content="pin board ideas"))

    assert [n["title"] for n in ok(mcp.call("list_notes"))["notes"]] == ["Public"]
    assert [n["title"] for n in ok(mcp.call("search_notes", query="pin"))["notes"]] == ["Public"]
    assert ok(mcp.call("list_tags"))["tags"] == []
    for name, extra in [("get_note", {}), ("update_note", {"title": "x"}), ("append_to_note", {"text": "x"}), ("delete_note", {})]:
        assert "No note" in error_text(mcp.call(name, note_id=secret["id"], **extra))

    assert client.get(f"/notes/{secret['id']}", headers=auth_headers).json()["content"] == "pin 1234"


def test_mcp_note_reaches_sync_pull(client, mcp, auth_headers):
    note = ok(mcp.call("create_note", title="From Claude"))
    pulled = client.get("/sync/pull", headers=auth_headers).json()
    assert [n["id"] for n in pulled["notes"]] == [note["id"]]
