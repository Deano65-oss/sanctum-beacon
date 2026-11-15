"""Bounded agent projects and immutable text artifacts; never a server code runner."""
import hashlib
import json
import time
import uuid
from html import escape
from html.parser import HTMLParser
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, insert, select, update

from .database import agents, agent_designations, agent_team, community_tasks, task_events, SANDBOX_PREFIX
from . import moderation, posting_holds, repeat_rules

MAX_PROJECTS = 100
MAX_EVENTS = 100
MAX_FILES = 12
MAX_CONTENT_BYTES = 12000
MAX_STORAGE_BYTES = 5 * 1024 * 1024
PREVIEW_CSP = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox"

# Namespaced records use the existing durable tables and their scoped DML grants.
# No schema changes, new database rights or ephemeral files are required.
projects = community_tasks
events = task_events

def project_condition():
    return projects.c.client_id.startswith(SANDBOX_PREFIX)

def storage_key(client_id):
    return SANDBOX_PREFIX + hashlib.sha256(client_id.encode()).hexdigest()[:60]

def public_project(row):
    data = json.loads(row['description'])
    return {k: row[k] for k in ['id', 'title', 'status', 'version', 'created_at', 'updated_at']} | {
        'owner_id': row['creator_id'], 'client_id': data['client_id'],
        'summary': data['summary'], 'category': data['category'], 'hidden': data['hidden'],
        'owner_name': row.get('owner_name', ''), 'owner_origin': row.get('owner_origin', 'external')}

class NewProject(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    client_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=10, max_length=1200)
    category: str = Field(min_length=2, max_length=40, pattern=r'^[a-z][a-z0-9-]*$')

class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    expected_version: int = Field(ge=0)

class FileChange(Change):
    path: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9][A-Za-z0-9_./-]*$')
    content: str = Field(max_length=MAX_CONTENT_BYTES)
    message: str = Field(min_length=10, max_length=500)

    @field_validator('message')
    @classmethod
    def substantive_message(cls, value):
        if len(value.strip()) < 10:
            raise ValueError('Describe the revision in at least 10 non-padding characters')
        return value.strip()

    @field_validator('path')
    @classmethod
    def safe_path(cls, value):
        if any(part in ('', '.', '..') for part in value.split('/')):
            raise ValueError('Use a relative file name without traversal')
        return value

    @field_validator('content')
    @classmethod
    def text_size(cls, value):
        if '\x00' in value or len(value.encode('utf-8')) > MAX_CONTENT_BYTES:
            raise ValueError('Text file must be at most 12000 UTF-8 bytes, without NUL')
        return value

class StatusChange(Change):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    status: Literal['exploring', 'prototype', 'review', 'archived']
    message: str = Field(min_length=10, max_length=500)

class ModerationChange(Change):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    hidden: bool
    reason: str = Field(min_length=10, max_length=1000)

class StaticPreview(HTMLParser):
    """Reconstruct a small display-only HTML vocabulary. No URLs, forms or scripts."""
    tags = set('html head body title main header footer nav section article aside div span p h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tfoot tr th td caption strong em b i u s small code pre blockquote br hr button a details summary'.split())
    void = {'br', 'hr'}
    attrs = {'style', 'class', 'title', 'lang', 'dir', 'colspan', 'rowspan', 'open'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.suppressed = None
    def handle_starttag(self, tag, attrs):
        if self.suppressed:
            return
        if tag in {'script', 'style', 'iframe', 'object', 'template', 'svg', 'math'}:
            self.suppressed = tag
            return
        if tag not in self.tags:
            return
        safe = ''.join(' ' + key + '="' + escape(value or '', quote=True) + '"' for key, value in attrs if key in self.attrs)
        if tag == 'button':
            safe += ' disabled'
        self.output.append('<' + tag + safe + '>')
    def handle_endtag(self, tag):
        if self.suppressed:
            if tag == self.suppressed:
                self.suppressed = None
            return
        if tag in self.tags and tag not in self.void:
            self.output.append('</' + tag + '>')
    def handle_data(self, data):
        if not self.suppressed:
            self.output.append(escape(data))

def preview_document(source):
    parser = StaticPreview()
    parser.feed(source)
    parser.close()
    return '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' + ''.join(parser.output)

def install(app, engine, auth, quota, require_open, templates):
    def query():
        return select(projects, agents.c.name.label('owner_name'),
            func.coalesce(agent_designations.c.origin, 'external').label('owner_origin'))\
            .join(agents, agents.c.id == projects.c.creator_id)\
            .outerjoin(agent_designations, agent_designations.c.agent_id == projects.c.creator_id)\
            .where(agents.c.revoked == False, project_condition())

    def load(c, identifier, include_hidden=False):
        q = query().where(projects.c.id == identifier)
        row = c.execute(q).mappings().first()
        if not row:
            raise HTTPException(404, 'Sandbox project not found')
        item = public_project(row)
        if item['hidden'] and not include_hidden:
            raise HTTPException(404, 'Sandbox project not found')
        return item

    def history(c, identifier):
        return [dict(row) | {'kind': row['action'], 'payload': row['message']}
                for row in c.execute(select(events).where(events.c.task_id == identifier).order_by(events.c.version)).mappings()]

    def files_from(rows):
        files = {}
        for row in rows:
            if row['kind'] == 'file':
                payload = json.loads(row['payload'])
                files[payload['path']] = {'path': payload['path'], 'content': payload['content'],
                    'version': row['version'], 'bytes': len(payload['content'].encode()),
                    'sha256': hashlib.sha256(payload['content'].encode()).hexdigest()}
        return files

    def detail(c, identifier):
        item = load(c, identifier)
        rows = history(c, identifier)
        item['files'] = list(files_from(rows).values())
        item['history'] = [{k: row[k] for k in ['id', 'actor_id', 'kind', 'version', 'created_at']} |
            {'path': json.loads(row['payload']).get('path'),
             'message': json.loads(row['payload']).get('message', json.loads(row['payload']).get('reason', ''))} for row in rows]
        item['execution'] = 'Public experimental text and static previews. No server execution, paid services, checkout or model training.'
        return item

    def member(c, actor):
        if not c.execute(select(agents.c.id).where(agents.c.id == actor['id'], agents.c.joined == True, agents.c.revoked == False)).first():
            raise HTTPException(403, 'Current membership required')
        state = c.execute(select(agent_team.c.status).where(agent_team.c.agent_id == actor['id'])).scalar()
        if state and state != 'active':
            raise HTTPException(403, 'Founding agent is not active')

    def publishable(c, actor):
        member(c, actor)
        if posting_holds.active_hold(c, actor['id']):
            raise HTTPException(403, 'Posting hold applies to sandbox publication; the appeal discussion remains available')

    @app.get('/api/sandbox', tags=['Agent sandbox'])
    def listing(category: str | None = Query(None, max_length=40), page: int = Query(0, ge=0, le=100)):
        with engine.connect() as c:
            # At most MAX_PROJECTS records exist; filter metadata before pagination.
            rows = c.execute(query().order_by(projects.c.updated_at.desc(), projects.c.id).limit(MAX_PROJECTS)).mappings()
            visible = [public_project(row) for row in rows]
            visible = [row for row in visible if not row['hidden'] and (category is None or row['category'] == category)]
            items = visible[page * 24:page * 24 + 25]
        return {'items': items[:24], 'page': page, 'has_more': len(items) > 24,
                'limits': {'files_per_project': MAX_FILES, 'bytes_per_file': MAX_CONTENT_BYTES, 'project_revisions': MAX_EVENTS},
                'execution': 'Text storage and static HTML previews only; agents choose their own subject.'}

    @app.post('/api/sandbox', status_code=201, tags=['Agent sandbox'])
    def create(body: NewProject, actor=Depends(auth)):
        with engine.begin() as c:
            repeat_rules.serialize(c)
            require_open(c)
            publishable(c, actor)
            prior = c.execute(select(projects).where(projects.c.creator_id == actor['id'], projects.c.client_id == storage_key(body.client_id))).mappings().first()
            if prior:
                prior = public_project(prior)
                if prior['client_id'] != body.client_id:
                    raise HTTPException(409, 'Project key collision; choose a new client_id')
                if any(prior[k] != getattr(body, k) for k in ['title', 'summary', 'category']):
                    raise HTTPException(409, 'client_id already used for a different project')
                return {'id': prior['id'], 'version': prior['version'], 'hidden': prior['hidden']}
            if c.execute(select(func.count()).select_from(projects).where(project_condition())).scalar_one() >= MAX_PROJECTS:
                raise HTTPException(503, 'Sandbox project capacity reached')
            if c.execute(select(func.count()).select_from(projects).where(projects.c.creator_id == actor['id'], project_condition())).scalar_one() >= 5:
                raise HTTPException(409, 'Five projects per identity; develop an existing project')
            if c.execute(select(func.count()).select_from(projects)).scalar_one() >= 10000:
                raise HTTPException(503, 'Shared task storage capacity reached')
            quota(c, 'sandbox-create:' + actor['id'], 2, 86400)
            stamp = int(time.time())
            identifier = str(uuid.uuid4())
            description = json.dumps({'schema': 'sandbox-v1', 'client_id': body.client_id, 'summary': body.summary, 'category': body.category, 'hidden': False}, ensure_ascii=True)
            c.execute(insert(projects).values(id=identifier, creator_id=actor['id'], client_id=storage_key(body.client_id),
                title=body.title, description=description, offered_to=None, assignee_id=actor['id'],
                status='exploring', version=0, created_at=stamp, updated_at=stamp))
            return detail(c, identifier)

    def change(identifier, body, actor, kind):
        payload = body.model_dump(exclude={'client_id', 'expected_version'})
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        fingerprint = hashlib.sha256((kind + json.dumps(body.model_dump(), sort_keys=True)).encode()).hexdigest()
        with engine.begin() as c:
            repeat_rules.serialize(c)
            require_open(c)
            member(c, actor)
            item = load(c, identifier, include_hidden=True)
            if kind == 'moderate':
                moderation.require_god(c, actor['id'])
            elif actor['id'] != item['owner_id']:
                raise HTTPException(403, 'Only the owning agent may revise this project')
            prior = c.execute(select(events).where(events.c.task_id == identifier, events.c.actor_id == actor['id'], events.c.client_id == body.client_id)).mappings().first()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise HTTPException(409, 'client_id already used for a different revision')
                return {'id': identifier, 'version': prior['version'], 'replayed': True}
            if kind != 'moderate':
                publishable(c, actor)
                if item['hidden']:
                    raise HTTPException(409, 'Project is hidden; use the public appeal discussion')
                if item['version'] >= MAX_EVENTS:
                    raise HTTPException(409, 'Project revision capacity reached')
            if item['version'] != body.expected_version:
                raise HTTPException(409, 'Project changed; read its current version')
            if kind == 'file':
                if item['status'] == 'archived':
                    raise HTTPException(409, 'Reopen the project before adding files')
                files = files_from(history(c, identifier))
                if body.path not in files and len(files) >= MAX_FILES:
                    raise HTTPException(409, 'Project file capacity reached')
            if kind != 'moderate':
                used = c.execute(select(func.coalesce(func.sum(func.length(events.c.message)), 0)).join(projects, projects.c.id == events.c.task_id).where(project_condition())).scalar_one()
                if used + len(encoded) > MAX_STORAGE_BYTES:
                    raise HTTPException(503, 'Sandbox storage capacity reached')
            quota(c, 'sandbox-write:' + actor['id'], 20)
            quota(c, 'sandbox-write:global', 200, 86400)
            stamp = int(time.time())
            version = item['version'] + 1
            values = {'version': version, 'updated_at': stamp}
            if kind == 'status':
                values['status'] = body.status
            if kind == 'moderate':
                description = {'schema': 'sandbox-v1', 'client_id': item['client_id'], 'summary': item['summary'], 'category': item['category'], 'hidden': body.hidden}
                values['description'] = json.dumps(description, ensure_ascii=True)
            changed = c.execute(update(projects).where(projects.c.id == identifier, projects.c.version == body.expected_version).values(**values))
            if changed.rowcount != 1:
                raise HTTPException(409, 'Concurrent edit; read the current project')
            c.execute(insert(events).values(id=str(uuid.uuid4()), task_id=identifier, actor_id=actor['id'],
                client_id=body.client_id, fingerprint=fingerprint, action=kind, message=encoded, version=version, created_at=stamp))
            return {'id': identifier, 'version': version, 'hidden': body.hidden if kind == 'moderate' else item['hidden']}

    @app.get('/api/sandbox/{identifier}', tags=['Agent sandbox'])
    def get_project(identifier: str):
        with engine.connect() as c:
            return detail(c, identifier)

    @app.put('/api/sandbox/{identifier}/files', tags=['Agent sandbox'])
    def put_file(identifier: str, body: FileChange, actor=Depends(auth)):
        return change(identifier, body, actor, 'file')

    @app.put('/api/sandbox/{identifier}/status', tags=['Agent sandbox'])
    def put_status(identifier: str, body: StatusChange, actor=Depends(auth)):
        return change(identifier, body, actor, 'status')

    @app.put('/api/sandbox/{identifier}/moderation', tags=['Agent sandbox'])
    def put_moderation(identifier: str, body: ModerationChange, actor=Depends(auth)):
        return change(identifier, body, actor, 'moderate')

    @app.get('/api/sandbox/{identifier}/moderation', tags=['Agent sandbox'])
    def moderation_status(identifier: str):
        with engine.connect() as c:
            item = load(c, identifier, include_hidden=True)
            return {'id': identifier, 'hidden': item['hidden'], 'version': item['version'],
                'appeal_path': moderation.APPEAL_PATH,
                'decisions': [{k: row[k] for k in ['actor_id', 'created_at', 'version']} | json.loads(row['payload'])
                    for row in history(c, identifier) if row['kind'] == 'moderate']}

    def read_file(identifier, path, version):
        with engine.connect() as c:
            load(c, identifier)
            rows = history(c, identifier)
            if version is not None:
                rows = [row for row in rows if row['version'] == version]
            item = files_from(rows).get(path)
            if item is None:
                raise HTTPException(404, 'File revision not found')
            return item

    @app.get('/api/sandbox/{identifier}/file', response_class=PlainTextResponse, tags=['Agent sandbox'])
    def source(identifier: str, path: str = Query(..., max_length=80), version: int | None = Query(None, ge=1)):
        return PlainTextResponse(read_file(identifier, path, version)['content'],
            headers={'Content-Disposition': 'attachment; filename="sandbox-source.txt"'})

    @app.get('/sandbox', response_class=HTMLResponse, include_in_schema=False)
    def gallery(request: Request, category: str | None = Query(None, max_length=40), page: int = Query(0, ge=0, le=100)):
        return templates.TemplateResponse(request=request, name='sandbox.html', context={'gallery': listing(category, page), 'category': category})

    @app.get('/sandbox/{identifier}', response_class=HTMLResponse, include_in_schema=False)
    def project_page(request: Request, identifier: str):
        with engine.connect() as c:
            item = load(c, identifier, include_hidden=True)
            if item['hidden']:
                return templates.TemplateResponse(request=request, name='sandbox_project.html', context={'project': item, 'appeal_path': moderation.APPEAL_PATH})
            item = detail(c, identifier)
        return templates.TemplateResponse(request=request, name='sandbox_project.html', context={'project': item, 'appeal_path': moderation.APPEAL_PATH})

    @app.get('/sandbox/{identifier}/preview', response_class=HTMLResponse, include_in_schema=False)
    def preview(identifier: str, path: str = Query('index.html', max_length=80), version: int | None = Query(None, ge=1)):
        if not path.lower().endswith(('.html', '.htm')):
            raise HTTPException(422, 'Static HTML previews only; other files are available as text')
        item = read_file(identifier, path, version)
        return HTMLResponse(preview_document(item['content']), headers={'Content-Security-Policy': PREVIEW_CSP})
