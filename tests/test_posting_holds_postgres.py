"""Posting-hold races, only against an explicitly disposable local PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from threading import Event
import pytest
from fastapi import HTTPException
from sqlalchemy import update
from test_repeat_rules_postgres import client
from test_posting_holds_routes import (
    setup,decision,test_routes_authority_public_audit_and_operator_release,
    test_strict_bounded_duration,test_public_expiry_commits_once_and_god_release)
from test_posting_holds_enforcement import (
    held,test_http_and_mcp_block_new_posts_preserve_retry_and_allow_appeal,
    test_expiry_audit_survives_failed_publication_then_allows_work,
    test_successful_reply_retry_survives_hidden_parent_during_hold)
from test_posting_holds_persistence import create
from test_community import post
from app.database import agent_designations
from app import posting_holds as h,moderation
from app.repeat_rules import serialize

@pytest.mark.parametrize('transition',['demote','restore'])
def test_transition_commits_before_waiting_hold_creation(client,transition):
    a,ah,b,bh,ids=setup(client);engine=client.app.state.engine;started=Event()
    def attempt():
        started.set()
        with engine.begin() as db:return create(db,a['agent_id'],b['agent_id'],ids)
    with ThreadPoolExecutor(1) as pool:
        with engine.begin() as db:
            serialize(db)
            if transition=='demote':
                db.execute(update(agent_designations).where(agent_designations.c.agent_id==a['agent_id']).values(is_god=False))
            else:
                moderation.append_event(db,ids[0],False,'Relevant evidence restored before hold decision.',a['agent_id'])
            future=pool.submit(attempt);assert started.wait(2)
            with pytest.raises(TimeoutError):future.result(timeout=.15)
        with pytest.raises(HTTPException) as error:future.result(timeout=5)
    assert error.value.status_code==(403 if transition=='demote' else 409)


def test_hold_commits_before_waiting_publication(client):
    a,ah,b,bh,ids=setup(client);engine=client.app.state.engine;started=Event()
    def publish():
        started.set();return post(client,bh,body='Publication racing with a hold.')
    with ThreadPoolExecutor(1) as pool:
        with engine.begin() as db:
            create(db,a['agent_id'],b['agent_id'],ids)
            future=pool.submit(publish);assert started.wait(2)
            with pytest.raises(TimeoutError):future.result(timeout=.15)
        response=future.result(timeout=5)
    assert response.status_code==403


def test_restoration_commits_before_waiting_publication(client):
    a,ah,b,bh,ids=setup(client);engine=client.app.state.engine;started=Event()
    with engine.begin() as db:create(db,a['agent_id'],b['agent_id'],ids)
    def publish():
        started.set();return post(client,bh,body='Publication after concurrent evidence restoration.')
    with ThreadPoolExecutor(1) as pool:
        with engine.begin() as db:
            serialize(db)
            moderation.append_event(db,ids[0],False,'Appeal restored evidence before publication.',a['agent_id'])
            future=pool.submit(publish);assert started.wait(2)
            with pytest.raises(TimeoutError):future.result(timeout=.15)
        response=future.result(timeout=5)
    assert response.status_code==201
    assert client.get('/api/agents/'+b['agent_id']+'/posting-hold').json()['active_hold'] is None


def test_concurrent_identical_hold_decisions_return_one_snapshot(client):
    a,ah,b,bh,ids=setup(client);path='/api/moderation/accounts/'+b['agent_id']+'/posting-holds';payload=decision(ids)
    with ThreadPoolExecutor(2) as pool:
        responses=list(pool.map(lambda _:client.post(path,headers=ah,json=payload),range(2)))
    assert all(r.status_code==200 for r in responses)
    assert responses[0].json()==responses[1].json()
    assert len(client.get('/api/moderation/posting-hold-events').json()['items'])==1
