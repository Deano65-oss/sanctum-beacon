"""Run the sandbox persistence boundaries on an explicitly disposable local PG."""
from test_repeat_rules_postgres import client
from test_sandbox import (
    test_real_project_revision_lifecycle_and_plain_download,
    test_permissions_pause_membership_and_revocation,
    test_paused_helper_cannot_publish,
    test_hold_blocks_new_publication_without_blocking_appeals,
    test_moderation_hides_all_content_and_restores_history,
    test_concurrent_revision_and_idempotent_retry,
    test_sizes_capacity_and_moderation_reserve,
    test_namespaced_storage_has_no_new_schema_or_task_escape,
)
