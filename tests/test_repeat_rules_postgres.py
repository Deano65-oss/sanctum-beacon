"""Run against a disposable local PostgreSQL via SANCTUM_REPEAT_TEST_PG only."""
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update
from app.main import create_app
from app.database import metadata, agent_designations, posts
from app import repeat_rules as repeat
from test_community import ADMIN
from test_repeat_rules_integration import (
    setup, test_atomic_hold_public_reason_restore_and_retry,
    test_rule_retry_cannot_reactivate_after_source_restore,
    test_permissions_mcp_author_notice_and_nonmatches,
    test_concurrent_publication_one_retained_hold_and_event,
    test_held_starter_receipt_is_author_only_and_recoverable,
    test_automatic_disable_audit_and_rule_decision_concurrency)

@pytest.fixture
def client():
    url=os.environ.get('SANCTUM_REPEAT_TEST_PG')
    if not url:pytest.skip('Disposable PostgreSQL not configured')
    assert '/sanctum_repeat_test?' in url and 'host=/private/tmp/sanctum-repeat-' in url
    app=create_app(url,'http://testserver',ADMIN)
    with TestClient(app) as c:yield c
    metadata.drop_all(app.state.engine);app.state.engine.dispose()

@pytest.mark.parametrize('transition',['demote','restore'])
def test_transition_commits_before_waiting_activation(client,transition):
    h,mh,nh,p,api,payload,result=setup(client)
    actor=result['rule']['actor_id'];engine=client.app.state.engine;started=Event()
    def activate():
        started.set()
        with engine.begin() as c:return repeat.activate(c,actor,p['id'],'Fresh rule after concurrent transition.')
    with ThreadPoolExecutor(1) as pool:
        with engine.begin() as c:
            repeat.serialize(c)
            if transition=='demote':
                c.execute(update(agent_designations).where(agent_designations.c.agent_id==actor).values(is_god=False))
            else:
                c.execute(update(posts).where(posts.c.id==p['id']).values(hidden=False))
                repeat.source_restored(c,p['id'])
            future=pool.submit(activate);assert started.wait(2)
            with pytest.raises(TimeoutError):future.result(timeout=.15)
        with pytest.raises(HTTPException) as error:future.result(timeout=5)
    assert error.value.status_code==(403 if transition=='demote' else 409)
