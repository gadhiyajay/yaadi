"""POST /mcp -- Yaadi as a remote MCP server, so Claude can read and write notes.

Streamable HTTP transport in its simplest allowed form: stateless (no
Mcp-Session-Id), every POST answered with plain JSON (no SSE), GET -> 405.
That's all a buffered Lambda Function URL can serve anyway. Hand-rolled
rather than the `mcp` SDK, whose session manager can't survive Mangum running
the ASGI lifespan once per invocation -- see docs/MCP_SERVER_PLAN.md.

Tools call the same functions as the REST routes (notes.*, folders.*), so
timestamps, soft deletes and the GSI1 sync bookkeeping are identical to an
edit made in the PWA, which picks MCP edits up on its next /sync/pull.

Named mcp_server, not mcp: app/ is flattened onto sys.path, so an `mcp.py`
here would shadow the `mcp` package.
"""
import inspect
import json
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

import folders
import notes
import security
from models import Folder, FolderCreate, Note, NoteCreate, NoteUpdate

# Newest first; anything else a client asks for gets the newest.
SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]

# Notes with this tag don't exist as far as MCP is concerned -- some notes
# hold secrets that shouldn't end up in an LLM conversation.
PRIVATE_TAG = "private"

# Well under DynamoDB's 400 KB item limit, so an oversized write fails with a
# readable message instead of a 500.
MAX_CONTENT_CHARS = 100_000
PREVIEW_CHARS = 200

INSTRUCTIONS = (
    "Yaadi is the user's personal notes app. Notes are Markdown, with optional tags "
    "and a folder. Search before creating, so you add to an existing note instead of "
    "duplicating it. To add a line or item to a note, use append_to_note. For any other "
    "edit, call get_note first and pass its updated_at as expected_updated_at to "
    'update_note. Folders can be given by name or by path, like "Work/AWS".'
)

FOLDER_HELP = 'Folder name, path like "Work/AWS", or id.'


def require_mcp_token(request: Request) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or not security.verify_mcp_token(token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing MCP token",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[Depends(require_mcp_token)])


# --- tool registry ---


class ToolError(Exception):
    """Goes back to the model as an isError tool result, not a JSON-RPC error,
    so it can read the message and correct itself."""


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Tool:
    name: str
    title: str
    args: type[BaseModel]
    run: Callable[[BaseModel], dict]
    annotations: dict

    def definition(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": inspect.cleandoc(self.run.__doc__),
            "inputSchema": self.args.model_json_schema(),
            "annotations": {"title": self.title, **self.annotations},
        }


TOOLS: dict[str, Tool] = {}


def tool(title: str, args: type[BaseModel], *, read_only: bool = False, destructive: bool = False):
    def register(fn):
        TOOLS[fn.__name__] = Tool(
            name=fn.__name__,
            title=title,
            args=args,
            run=fn,
            annotations={
                "readOnlyHint": read_only,
                "destructiveHint": destructive,
                "openWorldHint": False,
            },
        )
        return fn

    return register


# --- helpers ---


def _folder_tree() -> tuple[dict[str, Folder], dict[str, str]]:
    """(folder id -> Folder, folder id -> "Parent/Child" path) for live folders."""
    by_id = {f.id: f for f in folders.list_folders()}

    def path(folder: Folder, seen: frozenset) -> str:
        parent = by_id.get(folder.parent_folder_id)
        if parent is None or parent.id in seen:  # top level, orphan, or a cycle
            return folder.name
        return f"{path(parent, seen | {folder.id})}/{folder.name}"

    return by_id, {fid: path(f, frozenset({fid})) for fid, f in by_id.items()}


def _resolve_folder(ref: str, paths: dict[str, str]) -> Optional[str]:
    """Folder id for an id, full path or bare name; None for "/" (top level)."""
    if ref.strip() in ("", "/"):
        return None
    if ref in paths:
        return ref
    want = ref.strip().strip("/").lower()
    matches = [fid for fid, p in paths.items() if p.lower() == want] or [
        fid for fid, p in paths.items() if p.rsplit("/", 1)[-1].lower() == want
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolError(f"No folder named {ref!r}. Use list_folders to see them, or create_folder.")
    options = ", ".join(sorted(paths[m] for m in matches))
    raise ToolError(f"{ref!r} matches several folders ({options}). Use the full path.")


def _in_folder(folder_id: Optional[str], target: str, by_id: dict[str, Folder]) -> bool:
    """True if folder_id is target or anywhere below it."""
    seen = set()
    while folder_id and folder_id not in seen:
        if folder_id == target:
            return True
        seen.add(folder_id)
        folder_id = by_id[folder_id].parent_folder_id if folder_id in by_id else None
    return False


def _is_private(note: Note) -> bool:
    return any(t.lower() == PRIVATE_TAG for t in note.tags)


def _visible_notes() -> list[Note]:
    return [n for n in notes.list_notes() if not _is_private(n)]


def _get_visible(note_id: str) -> Note:
    try:
        note = notes.get_note(note_id)
    except HTTPException:
        note = None
    if note is None or _is_private(note):
        # A private note looks exactly like a missing one.
        raise ToolError(f"No note with id {note_id!r}. Use search_notes or list_notes to find it.")
    return note


def _filtered(folder: Optional[str], tag: Optional[str]) -> tuple[list[Note], dict[str, str]]:
    by_id, paths = _folder_tree()
    found = _visible_notes()
    if folder is not None:
        target = _resolve_folder(folder, paths)
        if target is None:
            found = [n for n in found if not n.folder_id]
        else:
            found = [n for n in found if _in_folder(n.folder_id, target, by_id)]
    if tag:
        want = tag.lstrip("#").lower()
        found = [n for n in found if want in (t.lower() for t in n.tags)]
    return found, paths


def _summary(note: Note, paths: dict[str, str], preview: Optional[str] = None) -> dict:
    return {
        "id": note.id,
        "title": note.title,
        "folder": paths.get(note.folder_id) if note.folder_id else None,
        "tags": note.tags,
        "updated_at": note.updated_at,
        "preview": preview if preview is not None else " ".join(note.content.split())[:PREVIEW_CHARS],
    }


def _full(note: Note, paths: dict[str, str]) -> dict:
    return {
        "id": note.id,
        "title": note.title,
        "folder": paths.get(note.folder_id) if note.folder_id else None,
        "tags": note.tags,
        "content": note.content,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


def _snippet(content: str, terms: list[str]) -> str:
    low = content.lower()
    hits = [i for t in terms if (i := low.find(t)) >= 0]
    if not hits:
        return " ".join(content.split())[:PREVIEW_CHARS]
    start = max(min(hits) - 60, 0)
    text = " ".join(content[start : start + PREVIEW_CHARS].split())
    return ("…" if start else "") + text


# --- tools ---


class SearchNotesArgs(BaseModel):
    query: str = Field(description="Words to look for; every word must appear (case-insensitive).")
    folder: Optional[str] = Field(None, description=FOLDER_HELP + " Includes subfolders.")
    tag: Optional[str] = None
    limit: int = Field(20, ge=1, le=100)


@tool("Search notes", SearchNotesArgs, read_only=True)
def search_notes(args: SearchNotesArgs) -> dict:
    """Find notes by words in their title, tags or text. Returns summaries with a
    snippet around the match; call get_note for the full text."""
    terms = args.query.lower().split()
    if not terms:
        raise ToolError("query is empty.")
    found, paths = _filtered(args.folder, args.tag)
    scored = []
    for note in found:
        title, body = note.title.lower(), note.content.lower()
        tags = " ".join(note.tags).lower()
        score = 0
        for term in terms:
            if term in title:
                score += 3
            elif term in tags:
                score += 2
            elif term in body:
                score += 1
            else:
                break
        else:
            scored.append((score, note))
    scored.sort(key=lambda s: (s[0], s[1].updated_at), reverse=True)
    return {
        "notes": [_summary(n, paths, _snippet(n.content, terms)) for _, n in scored[: args.limit]],
        "total_matches": len(scored),
    }


class ListNotesArgs(BaseModel):
    folder: Optional[str] = Field(None, description=FOLDER_HELP + ' Includes subfolders; "/" = top level only.')
    tag: Optional[str] = None
    limit: int = Field(30, ge=1, le=200)


@tool("List notes", ListNotesArgs, read_only=True)
def list_notes(args: ListNotesArgs) -> dict:
    """List notes, most recently updated first, optionally only in one folder or
    with one tag. Returns summaries; call get_note for the full text."""
    found, paths = _filtered(args.folder, args.tag)
    found.sort(key=lambda n: n.updated_at, reverse=True)
    return {"notes": [_summary(n, paths) for n in found[: args.limit]], "total": len(found)}


class NoteIdArgs(BaseModel):
    note_id: str


@tool("Read note", NoteIdArgs, read_only=True)
def get_note(args: NoteIdArgs) -> dict:
    """Get one note's full Markdown content. Its updated_at is what update_note
    needs as expected_updated_at."""
    note = _get_visible(args.note_id)
    return _full(note, _folder_tree()[1])


class CreateNoteArgs(BaseModel):
    title: str
    content: str = Field("", max_length=MAX_CONTENT_CHARS, description="Markdown.")
    folder: Optional[str] = Field(None, description=FOLDER_HELP + " Omit for top level.")
    tags: list[str] = Field(default_factory=list)


@tool("Create note", CreateNoteArgs)
def create_note(args: CreateNoteArgs) -> dict:
    """Create a new note. Search first if it might already exist; adding to an
    existing note is usually better than a near-duplicate."""
    paths = _folder_tree()[1]
    folder_id = _resolve_folder(args.folder, paths) if args.folder else None
    note = notes.create_note(
        NoteCreate(title=args.title, content=args.content, folder_id=folder_id, tags=args.tags)
    )
    return _full(note, paths)


class UpdateNoteArgs(BaseModel):
    note_id: str
    expected_updated_at: Optional[str] = Field(
        None, description="updated_at from get_note. The update is refused if the note changed since."
    )
    title: Optional[str] = None
    content: Optional[str] = Field(
        None, max_length=MAX_CONTENT_CHARS, description="Replaces the whole note text (Markdown)."
    )
    folder: Optional[str] = Field(None, description=FOLDER_HELP + ' "/" moves it to the top level.')
    tags: Optional[list[str]] = Field(None, description="Replaces all tags.")


@tool("Edit note", UpdateNoteArgs, destructive=True)
def update_note(args: UpdateNoteArgs) -> dict:
    """Change a note's title, content, folder or tags. Omitted fields stay as
    they are. content replaces the whole text, so read the note with get_note
    first and pass its updated_at as expected_updated_at."""
    note = _get_visible(args.note_id)
    if args.expected_updated_at and args.expected_updated_at != note.updated_at:
        raise ToolError(
            f"The note changed since you read it (updated_at is now {note.updated_at}). "
            "Call get_note again and redo the edit on the current text."
        )
    paths = _folder_tree()[1]
    changes = args.model_dump(include={"title", "content", "tags"}, exclude_none=True)
    if args.folder is not None:
        changes["folder_id"] = _resolve_folder(args.folder, paths)
    if not changes:
        raise ToolError("Nothing to change -- pass at least one of title, content, folder, tags.")
    return _full(notes.update_note(note.id, NoteUpdate(**changes)), paths)


class AppendToNoteArgs(BaseModel):
    note_id: str
    text: str = Field(max_length=MAX_CONTENT_CHARS, description="Markdown, added on a new line at the end.")


@tool("Add to note", AppendToNoteArgs)
def append_to_note(args: AppendToNoteArgs) -> dict:
    """Add text to the end of a note, e.g. an item to a list. The rest of the
    note is left untouched."""
    note = _get_visible(args.note_id)
    content = f"{note.content.rstrip()}\n{args.text}" if note.content.strip() else args.text
    if len(content) > MAX_CONTENT_CHARS:
        raise ToolError(f"The note would exceed {MAX_CONTENT_CHARS} characters; start a new note instead.")
    updated = notes.update_note(note.id, NoteUpdate(content=content))
    return _summary(updated, _folder_tree()[1])


@tool("Delete note", NoteIdArgs, destructive=True)
def delete_note(args: NoteIdArgs) -> dict:
    """Delete one note. It disappears from every device."""
    note = _get_visible(args.note_id)
    notes.delete_note(note.id)
    return {"deleted": note.id, "title": note.title}


class NoArgs(BaseModel):
    pass


@tool("List folders", NoArgs, read_only=True)
def list_folders(args: NoArgs) -> dict:
    """List every folder as a path, with how many notes are directly inside it."""
    _, paths = _folder_tree()
    counts = Counter(n.folder_id for n in _visible_notes())
    return {
        "folders": [
            {"id": fid, "path": path, "notes": counts[fid]}
            for fid, path in sorted(paths.items(), key=lambda p: p[1].lower())
        ],
        "top_level_notes": counts[None],
    }


class CreateFolderArgs(BaseModel):
    name: str = Field(min_length=1)
    parent: Optional[str] = Field(None, description=FOLDER_HELP + " Omit for top level.")


@tool("Create folder", CreateFolderArgs)
def create_folder(args: CreateFolderArgs) -> dict:
    """Create a folder, optionally inside another one."""
    paths = _folder_tree()[1]
    parent_id = _resolve_folder(args.parent, paths) if args.parent else None
    folder = folders.create_folder(FolderCreate(name=args.name, parent_folder_id=parent_id))
    path = f"{paths[parent_id]}/{folder.name}" if parent_id else folder.name
    return {"id": folder.id, "path": path}


@tool("List tags", NoArgs, read_only=True)
def list_tags(args: NoArgs) -> dict:
    """List every tag in use, most used first."""
    counts = Counter(t for n in _visible_notes() for t in n.tags)
    return {"tags": [{"tag": t, "notes": c} for t, c in counts.most_common()]}


# --- JSON-RPC ---


def _call_tool(name: str, arguments: dict) -> dict:
    t = TOOLS.get(name)
    if t is None:
        raise RpcError(-32602, f"Unknown tool: {name}")
    try:
        result = t.run(t.args.model_validate(arguments))
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'arguments'}: {err['msg']}" for err in e.errors())
        return _tool_error(f"Invalid arguments -- {problems}")
    except ToolError as e:
        return _tool_error(str(e))
    except HTTPException as e:  # e.g. the note was deleted mid-call
        return _tool_error(str(e.detail))
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "structuredContent": result,
        "isError": False,
    }


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "yaadi", "title": "Yaadi", "version": "1.0.0"},
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [t.definition() for t in TOOLS.values()]}
    if method == "tools/call":
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "arguments must be an object")
        return _call_tool(params.get("name"), arguments)
    raise RpcError(-32601, f"Method not found: {method}")


def _rpc_error(msg_id, code: int, message: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


@router.post("")
async def rpc(request: Request) -> Response:
    version = request.headers.get("mcp-protocol-version")
    if version and version not in SUPPORTED_VERSIONS:
        return _rpc_error(None, -32600, f"Unsupported MCP-Protocol-Version: {version}", 400)
    try:
        msg = json.loads(await request.body())
    except ValueError:
        return _rpc_error(None, -32700, "Parse error", 400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request", 400)
    if "id" not in msg:
        return Response(status_code=202)  # a notification, e.g. notifications/initialized

    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(msg["id"], -32602, "params must be an object")
    try:
        # boto3 calls block; keep them off the event loop.
        result = await run_in_threadpool(_dispatch, msg["method"], params)
    except RpcError as e:
        return _rpc_error(msg["id"], e.code, str(e))
    return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result})


@router.api_route("", methods=["GET", "DELETE"])
def no_stream() -> Response:
    # Stateless, JSON-only server: no SSE stream to open, no session to end.
    return Response(status_code=405, headers={"Allow": "POST"})
