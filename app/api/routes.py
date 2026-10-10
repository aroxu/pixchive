import secrets
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from app.api import contracts as c
from app.core.errors import DomainError
from app.core.query import (
    ArtworkQuery,
    DirectoryQuery,
    FileQuery,
    JobQuery,
    PageQuery,
    TagDirectoryQuery,
)
from app.services import (
    accounts,
    auth,
    directory,
    export,
    inventory,
    jobs,
    library,
    local_tags,
    preferences,
    previews,
    remote_history,
    repair,
    retention,
    schedule,
    storage,
    system,
    ugoira,
)

public = APIRouter(prefix="/api")
IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128)]
TagIdentity = Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807)]
ArtistIdentity = Annotated[str, Path(pattern=r"^[0-9]{1,20}$")]


def session_cookie(request: Request) -> str | None:
    return request.cookies.get("pixchive_session")


async def authorize(request: Request):
    session = auth.require_session(session_cookie(request))
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        value = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(value, session.csrf):
            raise DomainError("csrf_failed", "페이지를 새로고침한 뒤 다시 시도해 주세요.", 403)


protected = APIRouter(prefix="/api", dependencies=[Depends(authorize)])


def set_cookie(request: Request, response: Response, token: str):
    response.set_cookie(
        "pixchive_session",
        token,
        httponly=True,
        samesite="strict",
        secure=(
            request.app.state.settings.secure_cookie
            or request.headers.get("origin", "").startswith("https://")
        ),
        max_age=auth.SESSION_SECONDS,
        path="/",
    )


def attachment(filename: str) -> dict:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, no-store",
    }


@public.get("/session", response_model=c.SessionStatus)
async def session_status(request: Request):
    return auth.status(session_cookie(request))


@public.post("/setup", response_model=c.SessionStatus, status_code=201)
async def setup(body: c.Setup, request: Request, response: Response):
    auth.throttle(request.client.host if request.client else "unknown")
    token = auth.setup(request.app.state.settings, body.username, body.password, body.setup_token)
    set_cookie(request, response, token)
    return auth.status(token)


@public.post("/session", response_model=c.SessionStatus)
async def login(body: c.Login, request: Request, response: Response):
    auth.throttle(request.client.host if request.client else "unknown")
    token = auth.login(body.username, body.password)
    set_cookie(request, response, token)
    return auth.status(token)


@protected.delete("/session", status_code=204)
async def logout(request: Request, response: Response):
    auth.logout(session_cookie(request))
    response.delete_cookie("pixchive_session", path="/")


@protected.get("/accounts", response_model=list[c.Account])
async def account_list():
    return accounts.list_accounts()


@protected.put("/accounts/1/credential", response_model=c.Account)
async def credential(body: c.CredentialInput, request: Request):
    return accounts.store_credential(request.app.state.settings, body.phpsessid)


@protected.delete("/accounts/1/credential", status_code=204)
async def disconnect():
    accounts.disconnect()


@protected.get("/artworks", response_model=c.ArtworkList)
async def artworks(query: Annotated[ArtworkQuery, Query()]):
    return library.list_artworks(**query.model_dump())


@protected.post("/accounts/1/validate", response_model=c.JobView, status_code=202)
async def validate_account(idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]):
    return jobs.serialize(jobs.enqueue_account("validate", idempotency_key))


@protected.post("/jobs/sync", response_model=c.JobView, status_code=202)
async def sync(body: c.SyncInput, idempotency_key: IdempotencyKey):
    return jobs.serialize(jobs.enqueue_account("sync", idempotency_key, body.scope, mode=body.mode))


@protected.get("/schedule", response_model=c.ScheduleView)
async def schedule_view():
    return schedule.view()


@protected.patch("/schedule", response_model=c.ScheduleView)
async def schedule_edit(body: c.ScheduleInput):
    return schedule.update(body.model_dump())


@protected.get("/artworks/{artwork_id}", response_model=c.ArtworkDetail)
async def artwork(artwork_id: str):
    return library.detail(artwork_id)


@protected.get("/artworks/{artwork_id}/history", response_model=list[c.HistoryEntry])
async def artwork_history(artwork_id: str):
    return library.history(artwork_id)


@protected.get("/artworks/{artwork_id}/history/pages", response_model=c.HistoryList)
async def artwork_history_pages(artwork_id: str, query: Annotated[PageQuery, Query()]):
    return remote_history.listing(artwork_id, **query.model_dump())


@protected.get("/artworks/{artwork_id}/history/{snapshot_id}", response_model=c.HistorySnapshot)
async def artwork_snapshot(artwork_id: str, snapshot_id: TagIdentity):
    return remote_history.detail(artwork_id, snapshot_id)


@protected.get("/artworks/{artwork_id}/files", response_model=c.FileList)
async def artwork_files(artwork_id: str, query: Annotated[FileQuery, Query()]):
    return inventory.files(artwork_id, **query.model_dump())


@protected.get("/artworks/{artwork_id}/deletion", response_model=c.DeletionPreview)
async def deletion_preview(artwork_id: str):
    return retention.preview(artwork_id)


@protected.post("/artworks/{artwork_id}/delete", response_model=c.JobView, status_code=202)
async def delete_artwork(artwork_id: str, idempotency_key: IdempotencyKey):
    return jobs.serialize(retention.request_delete(artwork_id, idempotency_key))


@protected.post("/artworks/{artwork_id}/repair", response_model=c.JobView, status_code=202)
async def repair_artwork(artwork_id: str, idempotency_key: IdempotencyKey):
    return jobs.serialize(repair.request(artwork_id, idempotency_key))


@protected.post("/artworks/{artwork_id}/restore", response_model=c.RestoreResult)
async def restore_artwork(
    artwork_id: str, body: c.RestoreInput, response: Response, idempotency_key: IdempotencyKey
):
    job = retention.request_restore(artwork_id, idempotency_key, body.download)
    response.status_code = 202 if job else 200
    return {"artwork_id": artwork_id, "job": jobs.serialize(job) if job else None}


@protected.get("/exclusions", response_model=c.ExclusionList)
async def exclusions(
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return retention.exclusions(page, page_size)


@protected.get("/artworks/{artwork_id}/export")
async def export_artwork(artwork_id: str, request: Request):
    filename, stream = export.artwork_archive(request.app.state.settings, artwork_id)
    return StreamingResponse(stream, media_type="application/zip", headers=attachment(filename))


@protected.get("/export/metadata")
async def export_metadata():
    filename, stream = export.library_metadata()
    return StreamingResponse(stream, media_type="application/json", headers=attachment(filename))


@protected.get("/artworks/{artwork_id}/neighbors", response_model=c.ArtworkNeighbors)
async def artwork_neighbors(artwork_id: str, query: Annotated[ArtworkQuery, Query()]):
    return library.neighbors(artwork_id, query)


@protected.patch("/artworks/{artwork_id}", response_model=c.ArtworkDetail)
async def edit_note(artwork_id: str, body: c.NoteEdit):
    return library.edit_note(artwork_id, body.note)


@protected.patch("/artists/{artist_id}", response_model=c.ArtistView)
async def edit_alias(artist_id: ArtistIdentity, body: c.AliasEdit):
    return library.edit_alias(artist_id, body.alias)


@protected.post("/artworks/tags/bulk", status_code=204)
async def edit_tags(body: c.TagEdit):
    library.edit_tags(body.artwork_ids, body.name, body.action)


@protected.post("/artworks/{artwork_id}/convert", response_model=c.JobView, status_code=202)
async def convert(artwork_id: str, idempotency_key: IdempotencyKey):
    return jobs.serialize(ugoira.request_conversion(artwork_id, idempotency_key))


@protected.post("/artworks/{artwork_id}/preview", response_model=c.JobView, status_code=202)
async def regenerate_preview(artwork_id: str, idempotency_key: IdempotencyKey):
    return jobs.serialize(previews.request(artwork_id, idempotency_key))


@protected.get("/artists", response_model=list[c.ArtistView])
async def artists(query: Annotated[ArtworkQuery, Query()]):
    return library.artists(**query.model_dump())


@protected.get("/artists/pages", response_model=c.ArtistList)
async def artist_pages(query: Annotated[DirectoryQuery, Query()]):
    return directory.artist_listing(**query.model_dump())


@protected.get("/artists/{artist_id}/avatar")
async def artist_avatar(artist_id: ArtistIdentity, request: Request):
    path, media_type = storage.avatar(request.app.state.settings, artist_id)
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@protected.get("/artists/{artist_id}", response_model=c.ArtistDetail)
async def artist_detail(artist_id: ArtistIdentity):
    return directory.artist_detail(artist_id)


@protected.get("/tags", response_model=list[c.TagView])
async def tags(query: Annotated[ArtworkQuery, Query()]):
    return library.tags(**query.model_dump())


@protected.get("/tags/catalog", response_model=list[c.TagView])
async def tag_catalog():
    return local_tags.catalog()


@protected.get("/tags/catalog/pages", response_model=c.TagList)
async def tag_catalog_pages(query: Annotated[TagDirectoryQuery, Query()]):
    return directory.tag_listing(**query.model_dump())


@protected.post("/tags", response_model=c.TagView, status_code=201)
async def create_tag(body: c.TagNameInput):
    return local_tags.create(body.name)


@protected.get("/tags/{tag_id}", response_model=c.TagDetail)
async def tag_detail(tag_id: TagIdentity):
    return directory.tag_detail(tag_id)


@protected.get("/tags/{tag_id}/rename-preview", response_model=c.TagRenamePreview)
async def tag_rename_preview(
    tag_id: TagIdentity, name: Annotated[str, Query(min_length=1, max_length=100)]
):
    return local_tags.rename_preview(tag_id, name)


@protected.patch("/tags/{tag_id}", response_model=c.TagRenameResult)
async def rename_tag(tag_id: TagIdentity, body: c.TagNameInput):
    return local_tags.rename(tag_id, body.name)


@protected.get("/media/{asset_id}")
async def media(asset_id: str, request: Request):
    path, media_type = storage.media(request.app.state.settings, asset_id)
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@protected.get("/jobs", response_model=list[c.JobView])
async def job_list():
    return jobs.list_jobs()


@protected.get("/jobs/history", response_model=c.JobList)
async def job_history(query: Annotated[JobQuery, Query()]):
    return jobs.history(**query.model_dump())


@protected.get("/jobs/{job_id}", response_model=c.JobDetail)
async def job_detail(job_id: str):
    return jobs.detail(job_id)


@protected.get("/jobs/{job_id}/children", response_model=c.JobList)
async def job_children(job_id: str, query: Annotated[PageQuery, Query()]):
    return jobs.related(job_id, "children", **query.model_dump())


@protected.get("/jobs/{job_id}/parents", response_model=c.JobList)
async def job_parents(job_id: str, query: Annotated[PageQuery, Query()]):
    return jobs.related(job_id, "parents", **query.model_dump())


@protected.post("/jobs/integrity", response_model=c.JobView, status_code=202)
async def integrity(idempotency_key: IdempotencyKey):
    return jobs.serialize(jobs.enqueue_integrity(idempotency_key))


@protected.get("/jobs/{job_id}/issues", response_model=list[c.JobIssue])
async def job_issues(job_id: str):
    return jobs.issues(job_id)


@protected.get("/jobs/{job_id}/events", response_model=list[c.JobEventView])
async def job_events(job_id: str):
    return jobs.events(job_id)


@protected.get("/jobs/{job_id}/events/history", response_model=c.JobEventList)
async def job_event_history(job_id: str, query: Annotated[PageQuery, Query()]):
    return jobs.event_history(job_id, **query.model_dump())


@protected.get("/jobs/{job_id}/issues/history", response_model=c.JobIssueList)
async def job_issue_history(job_id: str, query: Annotated[PageQuery, Query()]):
    return jobs.issue_history(job_id, **query.model_dump())


@protected.post("/jobs/{job_id}/actions", response_model=c.JobView)
async def job_action(job_id: str, body: c.JobAction):
    return jobs.serialize(jobs.action(job_id, body.action))


@protected.get("/settings", response_model=c.SettingsView)
async def settings():
    return preferences.view()


@protected.patch("/settings", response_model=c.SettingsView)
async def edit_settings(body: c.SettingsInput):
    return preferences.update(body.model_dump(exclude_unset=True))


@protected.get("/system/status", response_model=c.SystemStatus)
async def system_status(request: Request):
    return system.status(request.app.state.settings)
