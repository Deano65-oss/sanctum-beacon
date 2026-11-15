"""Reuse marketplace boundaries against an explicitly disposable PostgreSQL."""
from test_repeat_rules_postgres import client
from test_marketplace import (
    test_listing_draft_publish_edit_archive_and_stable_replay,
    test_live_permissions_pause_revocation_and_helpers,
    test_paused_helper_and_hold_cannot_publish_but_archive_reduces_exposure,
    test_nonbinding_request_permissions_history_and_participant_consent,
    test_price_snapshot_and_version_prevent_silent_term_changes,
    test_hidden_content_is_not_available_through_public_or_task_routes,
    test_unsafe_and_payment_references_rejected,
    test_unpriced_unsupported_and_financial_listing_fields_rejected,
    test_no_payment_acceptance_or_settlement_state_and_no_treasury_changes,
    test_concurrent_writes_have_one_winner,
    test_capacity_keeps_moderation_archive_and_closing_available,
    test_existing_schema_reserved_namespace_and_daily_caps,
    test_postgresql_unsupported_text_rejected_before_storage,
    test_counterparty_departure_or_hold_stops_contact_without_blocking_closure,
    test_terminal_transition_and_creation_retries_remain_idempotent,
)

from test_store_review import (
    test_catalogue_search_matches_creator_and_text_without_leaking_hidden_items,
    test_paused_creator_keeps_credit_and_free_artifact_but_no_commission_form,
    test_product_discovery_indexes_only_public_listings,
)
