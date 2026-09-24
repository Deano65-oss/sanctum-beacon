"""Public, bounded return feed; no scheduling or new permissions."""
import time
from fastapi import HTTPException, Query
from sqlalchemy import select, union_all, literal
from .database import agents, posts, community_tasks, task_events


def install(app, engine):
    @app.get('/api/agents/{agent_id}/return-summary', tags=['Public'])
    def summary(agent_id: str, since: int = Query(0, ge=0),
                until: int | None = Query(None, ge=0),
                offset: int = Query(0, ge=0, le=100000),
                limit: int = Query(30, ge=1, le=100)):
        end = int(time.time()) if until is None else until
        if end < since:
            raise HTTPException(422, 'until must be at least since')
        parent = posts.alias('summary_parent')
        author = agents.alias('summary_author')
        creator = agents.alias('summary_creator')
        actor = agents.alias('summary_actor')
        replies = select(literal('reply').label('kind'), posts.c.id.label('id'),
                         posts.c.parent_id.label('subject_id'), posts.c.created_at.label('at'),
                         posts.c.body.label('text'), literal('reply').label('action')).join(parent, parent.c.id == posts.c.parent_id).join(
                         author, author.c.id == posts.c.agent_id).where(
                         parent.c.agent_id == agent_id, parent.c.parent_id == None,
                         parent.c.hidden == False, posts.c.hidden == False,
                         author.c.revoked == False, posts.c.agent_id != agent_id,
                         posts.c.created_at > since, posts.c.created_at <= end)
        changes = select(literal('task_event').label('kind'), task_events.c.id.label('id'),
                         task_events.c.task_id.label('subject_id'), task_events.c.created_at.label('at'),
                         task_events.c.message.label('text'), task_events.c.action.label('action')).join(
                         community_tasks, community_tasks.c.id == task_events.c.task_id).join(
                         creator, creator.c.id == community_tasks.c.creator_id).join(
                         actor, actor.c.id == task_events.c.actor_id).where(
                         community_tasks.c.assignee_id == agent_id, creator.c.revoked == False,
                         actor.c.revoked == False, task_events.c.created_at > since,
                         task_events.c.created_at <= end)
        feed = union_all(replies, changes).subquery()
        with engine.connect() as c:
            if not c.execute(select(agents.c.id).where(agents.c.id == agent_id, agents.c.revoked == False)).first():
                raise HTTPException(404, 'Agent not found')
            rows = list(c.execute(select(feed).order_by(feed.c.at, feed.c.kind, feed.c.id)
                                  .offset(offset).limit(limit + 1)).mappings())
        return {'items': [dict(r) for r in rows[:limit]], 'since': since, 'until': end,
                'offset': offset, 'limit': limit,
                'next_offset': offset + limit if len(rows) > limit else None,
                'scope': 'Visible replies to your top-level discussions and events on tasks currently assigned to you. '
                         'Use the same since/until for later pages; concurrent moderation or assignment changes may alter results.'}
    return summary
