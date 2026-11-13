"""Background jobs on TaskIQ over Redis Streams: the scheduler enqueues, workers run.

    taskiq worker english_agent.jobs.broker:broker english_agent.jobs.tasks
    taskiq scheduler english_agent.jobs.broker:scheduler english_agent.jobs.tasks

PostgreSQL stays the source of truth: every job re-checks its work under a database claim, so a
job delivered twice (Redis Streams are at-least-once) or re-enqueued by the next tick is a no-op.
Redis holds only the queue, short de-duplication keys, the shared Telegram rate budget and the
sitemap cache; losing it loses nothing the next minute's tick does not rebuild.
"""
