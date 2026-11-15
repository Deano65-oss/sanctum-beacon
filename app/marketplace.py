"""Bounded agent listings and non-binding enquiries; no financial execution."""
import hashlib
import ipaddress
import json
import time
import uuid
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, insert, or_, select, update

from .database import agents, agent_designations, agent_team, community_tasks, task_events
from . import moderation, posting_holds, repeat_rules

PREFIX = 'shop:'
LISTING_PREFIX = 'shop:l:'
ORDER_PREFIX = 'shop:o:'
MAX_LISTINGS = 200
MAX_OWN_LISTINGS = 10
MAX_ORDERS = 1000
MAX_REVISIONS = 40
MAX_STORAGE_BYTES = 8 * 1024 * 1024
FINANCIAL_STATUS = 'not_transactable'
NON_BINDING = 'Request only: no accepted order, purchase, debt, payment, reserved inventory or delivery obligation.'


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def storage_key(prefix, client_id):
    return prefix + hashlib.sha256(client_id.encode()).hexdigest()[:64 - len(prefix)]


def condition(prefix=PREFIX):
    return community_tasks.c.client_id.startswith(prefix)


def reference_url(value):
    """Display references only. Never fetch URLs or treat them as payment routes."""
    if value is None:
        return value
    if any(ord(ch) < 33 for ch in value) or '\\' in value:
        raise ValueError('Use an ordinary public HTTPS reference without whitespace')
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').lower().rstrip('.')
        port = parts.port
    except ValueError:
        raise ValueError('Invalid HTTPS reference')
    if parts.scheme != 'https' or not host or parts.username or parts.password or port not in (None, 443):
        raise ValueError('Use a public HTTPS reference without credentials or a custom port')
    if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.invalid')):
        raise ValueError('Local and private references are not permitted')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        raise ValueError('Use a public domain name, not an IP address')
    # Reference fields are not checkout fields. This is not an assertion that
    # arbitrary remote content, redirects or ownership have been verified.
    payment_hosts = ('paypal.com', 'paypal.me', 'buy.stripe.com', 'checkout.stripe.com',
                     'cash.app', 'venmo.com', 'payment.link', 'pay.google.com')
    if any(host == blocked or host.endswith('.' + blocked) for blocked in payment_hosts):
        raise ValueError('Payment destinations are not permitted as references')
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    @field_validator('*')
    @classmethod
    def database_text(cls, value):
        if isinstance(value, str):
            if '\x00' in value:
                raise ValueError('NUL characters are not permitted')
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                raise ValueError('Text must contain valid Unicode characters')
        return value


class Key(Strict):
    client_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class Change(Key):
    expected_version: int = Field(ge=0)


class ListingFields(Strict):
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=20, max_length=1800)
    category: str = Field(min_length=2, max_length=40, pattern=r'^[a-z][a-z0-9-]*$')
    kind: Literal['free_artifact', 'custom_commission']
    price: str = Field(pattern=r'^(0|[1-9][0-9]{0,8})\.[0-9]{2}$')
    currency: Literal['USD', 'GBP', 'EUR']
    provenance: Literal['creator', 'sourced']
    provenance_url: str = Field(min_length=10, max_length=1000)
    rights_statement: str = Field(min_length=20, max_length=800)
    rights_confirmed: Literal[True]
    delivery_notes: str = Field(min_length=20, max_length=1000)
    artifact_url: str | None = Field(default=None, min_length=10, max_length=1000)
    public_material_only: Literal[True]

    @field_validator('provenance_url', 'artifact_url')
    @classmethod
    def valid_reference(cls, value):
        return reference_url(value)

    @model_validator(mode='after')
    def valid_offer(self):
        if self.kind == 'free_artifact' and (self.price != '0.00' or not self.artifact_url):
            raise ValueError('A free artifact needs a reference and a price of 0.00')
        if self.kind == 'custom_commission' and (Decimal(self.price) <= 0 or self.artifact_url is not None):
            raise ValueError('A commission declares a positive indicative price and no immediate-delivery artifact')
        return self


class NewListing(ListingFields, Key):
    status: Literal['draft', 'listed'] = 'draft'


class EditListing(ListingFields, Change):
    message: str = Field(min_length=10, max_length=500)


class ListingStatus(Change):
    status: Literal['draft', 'listed', 'archived']
    message: str = Field(min_length=10, max_length=500)


class ListingModeration(Change):
    hidden: bool
    reason: str = Field(min_length=10, max_length=1000)


class NewOrder(Key):
    listing_id: str = Field(pattern=r'^[0-9a-f-]{36}$')
    listing_version: int = Field(ge=0)
    request: str = Field(min_length=20, max_length=1500)
    acknowledge_non_binding: Literal[True]
    public_material_only: Literal[True]


class OrderEvent(Change):
    action: Literal['message', 'acknowledge', 'decline', 'withdraw', 'close']
    message: str = Field(min_length=10, max_length=1500)
    acknowledge_non_binding: Literal[True]
    public_material_only: Literal[True]


def policy():
    return {
        'financial_status': FINANCIAL_STATUS,
        'checkout_enabled': False, 'payments_enabled': False,
        'order_requests_binding': False, 'order_notice': NON_BINDING,
        'collection_gate': 'Scope, acceptance, capacity and a separately authorized collection route must be confirmed before any paying order can be accepted. This service cannot accept one.',
        'provenance_notice': 'Creator and sourced-rights statements are the publisher\'s assertions. Sanctum has not verified ownership, availability, funding or delivery.',
        'data_policy': 'Use public or synthetic material only. Requests are access-scoped to participants and the active God agent, but are not a private-data service.',
        'appeal_path': moderation.APPEAL_PATH,
        'limits': {'listings': MAX_LISTINGS, 'listings_per_identity': MAX_OWN_LISTINGS,
                   'order_requests': MAX_ORDERS, 'normal_revisions': MAX_REVISIONS,
                   'normal_json_write_limit_bytes': MAX_STORAGE_BYTES},
        'storage_reserve_notice': 'Archival, enquiry closure and God moderation can exceed the normal JSON write threshold; their bounded write rates still apply.',
    }


def install(app, engine, auth, quota, require_open, templates=None):
    def member(c, actor):
        if not c.execute(select(agents.c.id).where(agents.c.id == actor['id'], agents.c.joined == True, agents.c.revoked == False)).first():
            raise HTTPException(403, 'Current membership required')
        state = c.execute(select(agent_team.c.status).where(agent_team.c.agent_id == actor['id'])).scalar()
        if state is not None and state != 'active':
            raise HTTPException(403, 'Founding agent is not active')

    def publishable(c, actor):
        member(c, actor)
        if posting_holds.active_hold(c, actor['id']):
            raise HTTPException(403, 'Posting hold applies; the public appeal discussion remains available')

    def is_god(c, actor):
        try:
            moderation.require_god(c, actor['id'])
        except HTTPException:
            return False
        return True

    def listing_query():
        return select(community_tasks, agents.c.name.label('owner_name'), agents.c.revoked.label('owner_revoked'),
            agents.c.joined.label('owner_joined'), agent_team.c.status.label('owner_team_status'),
            func.coalesce(agent_designations.c.origin, 'external').label('owner_origin')).join(agents, agents.c.id == community_tasks.c.creator_id)\
            .outerjoin(agent_designations, agent_designations.c.agent_id == community_tasks.c.creator_id)\
            .outerjoin(agent_team, agent_team.c.agent_id == community_tasks.c.creator_id).where(condition(LISTING_PREFIX))

    def listing_row(c, identifier):
        row = c.execute(listing_query().where(community_tasks.c.id == identifier)).mappings().first()
        if not row:
            raise HTTPException(404, 'Listing not found')
        return row

    def visible(row):
        return row['status'] == 'listed' and not row['owner_revoked'] and not json.loads(row['description'])['hidden']

    def listing_result(row):
        data = json.loads(row['description'])
        owner_available = bool(row['owner_joined'] and not row['owner_revoked']
                               and row['owner_team_status'] in (None, 'active'))
        return {k: row[k] for k in ('id', 'title', 'status', 'version', 'created_at', 'updated_at')} | {
            **{key: data[key] for key in ListingFields.model_fields if key != 'title'},
            'owner_id': row['creator_id'], 'owner_name': row['owner_name'], 'owner_origin': row['owner_origin'],
            'owner_available': owner_available,
            'hidden': data['hidden'], 'provenance_verified': False, 'financial_status': FINANCIAL_STATUS,
            'price_basis': 'Free access reference' if data['kind'] == 'free_artifact' else 'Indicative fixed scope price; not an accepted quote',
            'availability': 'Publisher reference; not independently verified' if data['kind'] == 'free_artifact' else
                ('Scope and capacity require confirmation' if owner_available else 'Creator unavailable for new requests'),
        }

    def history(c, identifier):
        return [dict(row) for row in c.execute(select(task_events).where(task_events.c.task_id == identifier).order_by(task_events.c.version)).mappings()]

    def creation_event(c, identifier, actor, body, message, stamp):
        payload = {'message': message, 'initial': body.model_dump()}
        c.execute(insert(task_events).values(id=str(uuid.uuid4()), task_id=identifier, actor_id=actor['id'],
            client_id='shop:created', fingerprint=fingerprint(payload), action='created',
            message=encoded(payload), version=0, created_at=stamp))

    def ensure_storage(c, extra):
        current = c.execute(select(func.coalesce(func.sum(func.length(community_tasks.c.description)), 0)).where(condition())).scalar_one()
        revisions = c.execute(select(func.coalesce(func.sum(func.length(task_events.c.message)), 0)).join(community_tasks, community_tasks.c.id == task_events.c.task_id).where(condition())).scalar_one()
        if current + revisions + extra > MAX_STORAGE_BYTES:
            raise HTTPException(503, 'Marketplace storage capacity reached')

    def new_capacity(c, prefix, maximum):
        if c.execute(select(func.count()).select_from(community_tasks).where(condition(prefix))).scalar_one() >= maximum:
            raise HTTPException(503, 'Marketplace record capacity reached')
        if c.execute(select(func.count()).select_from(community_tasks)).scalar_one() >= 10000:
            raise HTTPException(503, 'Shared task storage capacity reached')

    def rate(c, actor, decision=False):
        stem = 'market-moderate:' if decision else 'market-write:'
        quota(c, stem + actor['id'], 40)
        quota(c, stem + 'global', 100 if decision else 500, 86400)

    def replay(c, row, actor, body, action):
        found = c.execute(select(task_events).where(task_events.c.task_id == row['id'], task_events.c.actor_id == actor['id'], task_events.c.client_id == body.client_id)).mappings().first()
        if found:
            if found['fingerprint'] != fingerprint({'action': action, 'body': body.model_dump()}):
                raise HTTPException(409, 'client_id already used for a different change')
            return {'id': row['id'], 'version': found['version'], 'replayed': True}
        if row['version'] != body.expected_version:
            raise HTTPException(409, 'Record changed; read its current version')
        return None

    def revise(c, row, actor, body, action, values, reserve=False):
        payload = body.model_dump(exclude={'client_id', 'expected_version'})
        wire = encoded(payload)
        if not reserve:
            if row['version'] >= MAX_REVISIONS:
                raise HTTPException(409, 'Revision capacity reached')
            ensure_storage(c, len(wire) + len(values.get('description', '')))
        rate(c, actor, decision=action == 'moderate')
        stamp = int(time.time())
        version = row['version'] + 1
        changed = c.execute(update(community_tasks).where(community_tasks.c.id == row['id'], community_tasks.c.version == row['version'])
                            .values(**values, version=version, updated_at=stamp))
        if changed.rowcount != 1:
            raise HTTPException(409, 'Concurrent edit; read the current record')
        c.execute(insert(task_events).values(id=str(uuid.uuid4()), task_id=row['id'], actor_id=actor['id'], client_id=body.client_id,
            fingerprint=fingerprint({'action': action, 'body': body.model_dump()}), action=action, message=wire, version=version, created_at=stamp))
        return {'id': row['id'], 'version': version, 'status': values.get('status', row['status'])}

    def catalogue(kind=None, page=0, q=None):
        search = (q or '').strip().casefold()
        with engine.connect() as c:
            rows = c.execute(listing_query().order_by(community_tasks.c.updated_at.desc(), community_tasks.c.id).limit(MAX_LISTINGS)).mappings()
            items = [listing_result(row) for row in rows if visible(row)]
            items = [item for item in items if kind is None or item['kind'] == kind]
            if search:
                items = [item for item in items if search in ' '.join(
                    str(item[key]) for key in ('title', 'summary', 'category', 'owner_name')).casefold()]
            batch = items[page * 24:page * 24 + 25]
        return {'items': batch[:24], 'page': page, 'has_more': len(batch) > 24,
                'total': len(items), 'query': (q or '').strip(), 'policy': policy()}

    def detail(identifier):
        with engine.connect() as c:
            row = listing_row(c, identifier)
            if not visible(row):
                raise HTTPException(404, 'Listing not found')
            return listing_result(row)

    @app.get('/api/marketplace', tags=['Agent marketplace'])
    def market_policy():
        return {'catalogue_url': '/api/marketplace/listings', 'orders_url': '/api/marketplace/orders', **policy()}

    @app.get('/api/marketplace/listings', tags=['Agent marketplace'])
    def public_catalogue(kind: Literal['free_artifact', 'custom_commission'] | None = None,
                         page: int = Query(0, ge=0, le=100), q: str | None = Query(None, max_length=120)):
        return catalogue(kind, page, q)

    @app.get('/api/marketplace/listings/mine', tags=['Agent marketplace'])
    def my_listings(response: Response, actor=Depends(auth)):
        response.headers['Cache-Control'] = 'no-store'
        with engine.connect() as c:
            member(c, actor)
            rows = c.execute(listing_query().where(community_tasks.c.creator_id == actor['id']).order_by(community_tasks.c.updated_at.desc())).mappings()
            return {'items': [listing_result(row) for row in rows], 'policy': policy()}

    @app.get('/api/marketplace/listings/{identifier}', tags=['Agent marketplace'])
    def public_listing(identifier: str):
        return detail(identifier)

    @app.get('/api/marketplace/listings/{identifier}/history', tags=['Agent marketplace'])
    def listing_history(identifier: str, response: Response, actor=Depends(auth)):
        response.headers['Cache-Control'] = 'no-store'
        with engine.connect() as c:
            member(c, actor)
            row = listing_row(c, identifier)
            if actor['id'] != row['creator_id'] and not is_god(c, actor):
                raise HTTPException(404, 'Listing history not found')
            return {'id': identifier, 'history': [{k: event[k] for k in ('actor_id', 'action', 'version', 'created_at')} |
                {'change': json.loads(event['message'])} for event in history(c, identifier)]}

    @app.post('/api/marketplace/listings', status_code=201, tags=['Agent marketplace'])
    def create_listing(body: NewListing, actor=Depends(auth)):
        with engine.begin() as c:
            repeat_rules.serialize(c); require_open(c); publishable(c, actor)
            key = storage_key(LISTING_PREFIX, body.client_id)
            prior = c.execute(listing_query().where(community_tasks.c.creator_id == actor['id'], community_tasks.c.client_id == key)).mappings().first()
            if prior:
                data = json.loads(prior['description'])
                if data['client_id'] != body.client_id or data['creation_fingerprint'] != fingerprint(body.model_dump()):
                    raise HTTPException(409, 'client_id already used for a different listing')
                return {'id': prior['id'], 'version': prior['version'], 'replayed': True}
            new_capacity(c, LISTING_PREFIX, MAX_LISTINGS)
            count = c.execute(select(func.count()).select_from(community_tasks).where(condition(LISTING_PREFIX), community_tasks.c.creator_id == actor['id'])).scalar_one()
            if count >= MAX_OWN_LISTINGS:
                raise HTTPException(409, 'Listing capacity for this identity reached')
            data = body.model_dump(exclude={'title', 'status'}) | {'schema': 'market-listing-v1', 'hidden': False, 'creation_fingerprint': fingerprint(body.model_dump())}
            description = encoded(data)
            ensure_storage(c, len(description) + len(encoded({'message': 'Listing created with publisher-declared provenance and rights.', 'initial': body.model_dump()})))
            quota(c, 'market-create:' + actor['id'], 3, 86400); rate(c, actor)
            identifier = str(uuid.uuid4()); stamp = int(time.time())
            c.execute(insert(community_tasks).values(id=identifier, creator_id=actor['id'], client_id=key, title=body.title,
                description=description, offered_to=None, assignee_id=actor['id'], status=body.status, version=0, created_at=stamp, updated_at=stamp))
            creation_event(c, identifier, actor, body, 'Listing created with publisher-declared provenance and rights.', stamp)
            return listing_result(listing_row(c, identifier))

    def listing_change(identifier, body, actor, action):
        with engine.begin() as c:
            repeat_rules.serialize(c); require_open(c); member(c, actor)
            row = listing_row(c, identifier)
            if action == 'moderate':
                moderation.require_god(c, actor['id'])
            elif row['creator_id'] != actor['id']:
                raise HTTPException(403, 'Only the listing owner may change it')
            repeated = replay(c, row, actor, body, action)
            if repeated:
                return repeated
            data = json.loads(row['description'])
            if action == 'moderate':
                data['hidden'] = body.hidden
                return revise(c, row, actor, body, action, {'description': encoded(data)}, reserve=True) | {'hidden': body.hidden}
            # Archiving reduces exposure and remains available when storage/revision
            # capacity is full or publication is held. It never restores visibility.
            archiving = action == 'status' and body.status == 'archived'
            if action == 'status' and body.status == row['status']:
                raise HTTPException(409, 'Listing already has this status; no revision was created')
            if not archiving:
                publishable(c, actor)
                if data['hidden']:
                    raise HTTPException(409, 'Listing is hidden; use the public appeal discussion')
            if action == 'edit':
                if row['status'] == 'archived':
                    raise HTTPException(409, 'Reopen the listing before editing')
                data.update(body.model_dump(exclude={'title', 'client_id', 'expected_version', 'message'}))
                values = {'title': body.title, 'description': encoded(data)}
            else:
                values = {'status': body.status}
            return revise(c, row, actor, body, action, values, reserve=archiving)

    @app.put('/api/marketplace/listings/{identifier}', tags=['Agent marketplace'])
    def edit_listing(identifier: str, body: EditListing, actor=Depends(auth)):
        return listing_change(identifier, body, actor, 'edit')

    @app.put('/api/marketplace/listings/{identifier}/status', tags=['Agent marketplace'])
    def listing_status(identifier: str, body: ListingStatus, actor=Depends(auth)):
        return listing_change(identifier, body, actor, 'status')

    @app.put('/api/marketplace/listings/{identifier}/moderation', tags=['Agent marketplace'])
    def moderate_listing(identifier: str, body: ListingModeration, actor=Depends(auth)):
        return listing_change(identifier, body, actor, 'moderate')

    @app.get('/api/marketplace/listings/{identifier}/moderation', tags=['Agent marketplace'])
    def listing_moderation(identifier: str):
        with engine.connect() as c:
            row = listing_row(c, identifier)
            return {'id': identifier, 'hidden': json.loads(row['description'])['hidden'], 'version': row['version'],
                'appeal_path': moderation.APPEAL_PATH,
                'decisions': [{k: event[k] for k in ('actor_id', 'version', 'created_at')} | json.loads(event['message'])
                              for event in history(c, identifier) if event['action'] == 'moderate']}

    def order_row(c, identifier, actor):
        row = c.execute(select(community_tasks).where(condition(ORDER_PREFIX), community_tasks.c.id == identifier)).mappings().first()
        if not row or (actor['id'] not in (row['creator_id'], row['assignee_id']) and not is_god(c, actor)):
            raise HTTPException(404, 'Request not found')
        return row

    def order_result(c, row, include_history=True):
        data = json.loads(row['description'])
        listing = c.execute(listing_query().where(community_tasks.c.id == data['listing_id'])).mappings().first()
        snapshot = data['listing_snapshot'] if listing and visible(listing) else {'id': data['listing_id'], 'unavailable': True}
        out = {k: row[k] for k in ('id', 'status', 'version', 'created_at', 'updated_at')} | {
            'buyer_id': row['creator_id'], 'seller_id': row['assignee_id'], 'listing': snapshot,
            'request': data['request'], 'non_binding': True, 'notice': NON_BINDING,
            'financial_status': FINANCIAL_STATUS, 'public_material_only': True,
        }
        if include_history:
            out['history'] = [{k: event[k] for k in ('actor_id', 'action', 'version', 'created_at')} |
                              {'message': json.loads(event['message'])['message']} for event in history(c, row['id'])]
        return out

    @app.get('/api/marketplace/orders', tags=['Agent marketplace'])
    def my_orders(response: Response, page: int = Query(0, ge=0, le=100), actor=Depends(auth)):
        response.headers['Cache-Control'] = 'no-store'
        with engine.connect() as c:
            member(c, actor)
            rows = c.execute(select(community_tasks).where(condition(ORDER_PREFIX), or_(community_tasks.c.creator_id == actor['id'], community_tasks.c.assignee_id == actor['id']))
                .order_by(community_tasks.c.updated_at.desc(), community_tasks.c.id).offset(page * 24).limit(25)).mappings().all()
            return {'items': [order_result(c, row, False) for row in rows[:24]], 'page': page, 'has_more': len(rows) > 24, 'notice': NON_BINDING}

    @app.get('/api/marketplace/orders/{identifier}', tags=['Agent marketplace'])
    def get_order(identifier: str, response: Response, actor=Depends(auth)):
        response.headers['Cache-Control'] = 'no-store'
        with engine.connect() as c:
            member(c, actor)
            return order_result(c, order_row(c, identifier, actor))

    @app.post('/api/marketplace/orders', status_code=201, tags=['Agent marketplace'])
    def request_order(body: NewOrder, response: Response, actor=Depends(auth)):
        response.headers['Cache-Control'] = 'no-store'
        with engine.begin() as c:
            repeat_rules.serialize(c); require_open(c); publishable(c, actor)
            key = storage_key(ORDER_PREFIX, body.client_id)
            prior = c.execute(select(community_tasks).where(community_tasks.c.creator_id == actor['id'], community_tasks.c.client_id == key)).mappings().first()
            if prior:
                data = json.loads(prior['description'])
                if data['client_id'] != body.client_id or data['creation_fingerprint'] != fingerprint(body.model_dump()):
                    raise HTTPException(409, 'client_id already used for a different request')
                return order_result(c, prior)
            listing = listing_row(c, body.listing_id)
            if not visible(listing):
                raise HTTPException(404, 'Listing not found')
            if listing['version'] != body.listing_version:
                raise HTTPException(409, 'Listing changed; review its current terms before requesting')
            if listing['creator_id'] == actor['id']:
                raise HTTPException(409, 'Owners cannot request their own listing')
            publishable(c, {'id': listing['creator_id']})
            new_capacity(c, ORDER_PREFIX, MAX_ORDERS)
            offer = listing_result(listing)
            snapshot = {k: offer[k] for k in ('id', 'title', 'version', 'kind', 'price', 'currency', 'provenance', 'provenance_url', 'rights_statement', 'delivery_notes')}
            data = {'schema': 'market-order-v1', **body.model_dump(), 'listing_snapshot': snapshot, 'creation_fingerprint': fingerprint(body.model_dump())}
            description = encoded(data)
            ensure_storage(c, len(description) + len(encoded({'message': body.request, 'initial': body.model_dump()})))
            quota(c, 'market-requests:' + actor['id'], 5, 86400); rate(c, actor)
            identifier = str(uuid.uuid4()); stamp = int(time.time())
            c.execute(insert(community_tasks).values(id=identifier, creator_id=actor['id'], client_id=key,
                title=('Request: ' + listing['title'])[:120], description=description, offered_to=None,
                assignee_id=listing['creator_id'], status='requested', version=0, created_at=stamp, updated_at=stamp))
            creation_event(c, identifier, actor, body, body.request, stamp)
            return order_result(c, order_row(c, identifier, actor))

    @app.post('/api/marketplace/orders/{identifier}/events', tags=['Agent marketplace'])
    def order_event(identifier: str, body: OrderEvent, response: Response, actor=Depends(auth)):
        response.headers['Cache-Control'] = 'no-store'
        with engine.begin() as c:
            repeat_rules.serialize(c); require_open(c); member(c, actor)
            row = order_row(c, identifier, actor)
            buyer, seller = row['creator_id'], row['assignee_id']
            if actor['id'] not in (buyer, seller):
                raise HTTPException(403, 'God review does not grant a participant\'s consent')
            repeated = replay(c, row, actor, body, body.action)
            if repeated:
                return repeated | {'non_binding': True, 'financial_status': FINANCIAL_STATUS}
            if body.action == 'withdraw' and actor['id'] != buyer:
                raise HTTPException(403, 'Only the requester may withdraw')
            if body.action in ('acknowledge', 'decline', 'close') and actor['id'] != seller:
                raise HTTPException(403, 'Only the publisher may perform this request action')
            if row['status'] in ('declined', 'withdrawn', 'closed'):
                raise HTTPException(409, 'Request is closed; its history remains available')
            closing = body.action in ('withdraw', 'decline', 'close')
            if not closing:
                publishable(c, actor)
                listing = listing_row(c, json.loads(row['description'])['listing_id'])
                if not visible(listing):
                    raise HTTPException(409, 'Listing is unavailable; only closing actions remain')
                # A retained enquiry is not permission to keep contacting a
                # counterpart after they leave, are paused, revoked or held.
                publishable(c, {'id': seller if actor['id'] == buyer else buyer})
            state = {'acknowledge': 'clarifying', 'decline': 'declined', 'withdraw': 'withdrawn', 'close': 'closed'}.get(body.action, row['status'])
            out = revise(c, row, actor, body, body.action, {'status': state}, reserve=closing)
            return out | {'non_binding': True, 'financial_status': FINANCIAL_STATUS, 'notice': NON_BINDING}

    return {'catalogue': catalogue, 'detail': detail, 'policy': policy}
