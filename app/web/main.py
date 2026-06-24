"""FastAPI application factory and entry point."""

from __future__ import annotations

# T29 diagnostic — when PERSONA_FAULTHANDLER=1, dump ALL thread stacks every
# 5s to ~/.persona/faulthandler.log. During a hang, consecutive dumps show
# the event loop stuck in the same blocking call. Harmless when the env is
# unset. Guarded import so it costs nothing in normal operation.
import os as _os

if _os.environ.get("PERSONA_FAULTHANDLER") == "1":  # pragma: no cover
    import faulthandler as _faulthandler

    _fh_path = _os.path.join(_os.path.expanduser("~"), ".persona", "faulthandler.log")
    _fh_file = open(_fh_path, "w", buffering=1)  # noqa: SIM115
    _faulthandler.dump_traceback_later(5, repeat=True, file=_fh_file)

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware

from app.logging_setup import configure_logging, get_logger
from app.settings import get_settings
from app.storage.db import init_database
from app.web.middleware.api_auth import ApiAuthMiddleware
from app.web.routes.setup_gate import SetupGateMiddleware
from app.web.middleware.auth_gate import AuthGateMiddleware
from app.web.routes import (
    about as about_routes,
    analysis as analysis_routes,
    annotations as annotations_routes,
    annotations_csv as annotations_csv_routes,
    annotations_ndjson as annotations_ndjson_routes,
    api_tokens as api_tokens_routes,
    app_aliases as app_aliases_routes,
    app_calendar as app_calendar_routes,
    app_capture_skip as app_capture_skip_routes,
    app_shots_csv as app_shots_csv_routes,
    app_groups as app_groups_routes,
    app_health as app_health_routes,
    app_icons as app_icons_routes,
    app_icons_admin as app_icons_admin_routes,
    app_overrides as app_overrides_routes,
    app_retention as app_retention_routes,
    app_stats as app_stats_routes,
    archive as archive_routes,
    archive_bundle as archive_bundle_routes,
    archive_browse as archive_browse_routes,
    auto_collections as auto_collections_routes,
    audio_day as audio_day_routes,
    audio_search as audio_search_routes,
    audio_segment as audio_segment_routes,
    audio_settings as audio_settings_routes,
    audio_stats as audio_stats_routes,
    audit as audit_routes,
    agent_api as agent_api_routes,
    agents_admin as agents_admin_routes,
    audit_replay as audit_replay_routes,
    audit_rss as audit_rss_routes,
    audit_timeline as audit_timeline_routes,
    auto_tag as auto_tag_routes,
    budget as budget_routes,
    bookmarklet as bookmarklet_routes,
    bulk as bulk_routes,
    bulk_collection_add as bulk_collection_add_routes,
    bulk_pin as bulk_pin_routes,
    bulk_untag as bulk_untag_routes,
    bulk_delete as bulk_delete_routes,
    bulk_favourite as bulk_favourite_routes,
    cal_nav as cal_nav_routes,
    calendar as calendar_routes,
    capture_api,
    capture_weekly_trend as capture_weekly_trend_routes,
    clipboard as clipboard_routes,
    collection_visit_stats as collection_visit_stats_routes,
    companion as companion_routes,
    corpus_search as corpus_search_routes,
    csv_export,
    daily_digests as daily_digests_routes,
    dashboard as dashboard_routes,
    dashboard_tiles as dashboard_tiles_routes,
    dashboard_widgets as dashboard_widgets_routes,
    day_collage as day_collage_routes,
    day_json as day_json_routes,
    day_overview_page as day_overview_page_routes,
    analytics_page as analytics_page_routes,
    day_kanban as day_kanban_routes,
    day_ocr_diff as day_ocr_diff_routes,
    day_scrubber as day_scrubber_routes,
    day_tldr as day_tldr_routes,
    dedup_cluster as dedup_cluster_routes,
    diag_bundle as diag_bundle_routes,
    diff_picker as diff_picker_routes,
    diff_slider as diff_slider_routes,
    drag_to_tag as drag_to_tag_routes,
    dup_suggest as dup_suggest_routes,
    digest as digest_routes,
    digest_card as digest_card_routes,
    digest_prompts as digest_prompts_routes,
    system_prompt_settings as system_prompt_routes,
    mac_fs_settings as mac_fs_routes,
    profile_settings as profile_routes,
    memory_settings as memory_settings_routes,
    privacy_settings as privacy_settings_routes,
    briefing as briefing_routes,
    integrations_settings as integrations_settings_routes,
    skills_settings as skills_settings_routes,
    voice_chat as voice_chat_routes,
    alice as alice_routes,
    root_control as root_control_routes,
    activity_page as activity_page_routes,
    doctor as doctor_routes,
    embeddings_reindex as embeddings_reindex_routes,
    embeddings_stats as embeddings_stats_routes,
    embeddings_status,
    encrypted_notes as encrypted_notes_routes,
    export,
    external_ping as external_ping_routes,
    facet_sets as facet_sets_routes,
    favourites as favourites_routes,
    feature_index as feature_index_routes,
    feed_tokens as feed_tokens_routes,
    focus as focus_routes,
    focus_blocklist as focus_blocklist_routes,
    full_export as full_export_routes,
    health,
    health_dashboard as health_dashboard_routes,
    heatmap as heatmap_routes,
    hour_histogram as hour_histogram_routes,
    help as help_routes,
    ics_export as ics_export_routes,
    icons as icons_routes,
    import_screenshot as import_screenshot_routes,
    idle_stats as idle_stats_routes,
    idle_week as idle_week_routes,
    inbox as inbox_routes,
    journal as journal_routes,
    journal_export as journal_export_routes,
    kanban_csv as kanban_csv_routes,
    kbd_shortcuts as kbd_shortcuts_routes,
    keywords as keywords_routes,
    lang_autodetect as lang_autodetect_routes,
    live_sse as live_sse_routes,
    llm_switcher as llm_switcher_routes,
    llm_usage as llm_usage_routes,
    mobile as mobile_routes,
    monthly_digest_card as monthly_digest_card_routes,
    monthly_digests as monthly_digests_routes,
    monthly_stats_csv as monthly_stats_csv_routes,
    multi_day_diff as multi_day_diff_routes,
    multi_shot_zip as multi_shot_zip_routes,
    note_assist as note_assist_routes,
    note_attachments as note_attachments_routes,
    note_templates as note_templates_routes,
    notes as notes_routes,
    notes_csv_import as notes_csv_import_routes,
    notes_link_checker as notes_link_checker_routes,
    notes_search as notes_search_routes,
    notes_timeline as notes_timeline_routes,
    ocr_status,
    palette as palette_routes,
    pdf_export as pdf_export_routes,
    per_app_digest as per_app_digest_routes,
    personal_metrics as personal_metrics_routes,
    permalinks as permalinks_routes,
    phrase_autotag_suggest as phrase_autotag_suggest_routes,
    phrase_frequency as phrase_frequency_routes,
    pin as pin_routes,
    ping_heatmap as ping_heatmap_routes,
    pinmap as pinmap_routes,
    process_remap as process_remap_routes,
    public_day as public_day_routes,
    push_notif as push_notif_routes,
    qa as qa_routes,
    qr as qr_routes,
    query_collections as query_collections_routes,
    query_api as query_api_routes,
    quiet_hours as quiet_hours_routes,
    regex_rules as regex_rules_routes,
    random_shot as random_shot_routes,
    range_timeline as range_timeline_routes,
    reading as reading_routes,
    reading_mode as reading_mode_routes,
    reading_time as reading_time_routes,
    recycle as recycle_routes,
    redaction as redaction_routes,
    reminders as reminders_routes,
    rotate_gallery as rotate_gallery_routes,
    retention_preview as retention_preview_routes,
    retention_trend as retention_trend_routes,
    ocr_admin as ocr_admin_routes,
    ocr_language_stats as ocr_language_stats_routes,
    ocr_languages as ocr_languages_routes,
    ocr_diff as ocr_diff_routes,
    ocr_edit as ocr_edit_routes,
    ocr_emails as ocr_emails_routes,
    ocr_error_rate as ocr_error_rate_routes,
    ocr_find_replace as ocr_find_replace_routes,
    ocr_history as ocr_history_routes,
    ocr_length_chart as ocr_length_chart_routes,
    ocr_near_dup as ocr_near_dup_routes,
    ocr_overlay as ocr_overlay_routes,
    ocr_phones as ocr_phones_routes,
    ocr_phrase_tags as ocr_phrase_tags_routes,
    ocr_rerun_n as ocr_rerun_n_routes,
    ocr_retry as ocr_retry_routes,
    ocr_skip as ocr_skip_routes,
    ocr_translate as ocr_translate_routes,
    ocr_txt_export as ocr_txt_export_routes,
    ocr_vision as ocr_vision_routes,
    ocr_vision_replace as ocr_vision_replace_routes,
    rss as rss_routes,
    rss_index as rss_index_routes,
    saved_searches as saved_searches_routes,
    screenshot,
    screenshot_crop as screenshot_crop_routes,
    screenshot_frame as screenshot_frame_routes,
    search as search_routes,
    search_autocomplete as search_autocomplete_routes,
    search_facets as search_facets_routes,
    search_query_stats as search_query_stats_routes,
    search_tag_all as search_tag_all_routes,
    sparkline_svg as sparkline_svg_routes,
    semantic_similar as semantic_similar_routes,
    share as share_routes,
    share_analytics as share_analytics_routes,
    share_visits_csv as share_visits_csv_routes,
    shot_of_day as shot_of_day_routes,
    shot_of_week as shot_of_week_routes,
    shot_colours as shot_colours_routes,
    shot_share as shot_share_routes,
    shot_summary as shot_summary_routes,
    shot_token_cloud as shot_token_cloud_routes,
    sitemap as sitemap_routes,
    slack_summary as slack_summary_routes,
    shot_dimensions as shot_dimensions_routes,
    shot_embed as shot_embed_routes,
    shot_groups as shot_groups_routes,
    side_by_side as side_by_side_routes,
    shot_lock as shot_lock_routes,
    shot_share_ui as shot_share_ui_routes,
    share_collection as share_collection_routes,
    share_collection_pdf as share_collection_pdf_routes,
    share_collection_zip as share_collection_zip_routes,
    settings as settings_routes,
    settings_api as settings_api_routes,
    settings_backup as settings_backup_routes,
    sentiment_stats as sentiment_stats_routes,
    settings_diff as settings_diff_routes,
    setup as setup_routes,
    smtp_settings as smtp_settings_routes,
    stats,
    stats_csv as stats_csv_routes,
    storage_report as storage_report_routes,
    storage_savings as storage_savings_routes,
    stickers_gallery as stickers_gallery_routes,
    sticky_export as sticky_export_routes,
    sticky_notes as sticky_notes_routes,
    sticky_search as sticky_search_routes,
    streak as streak_routes,
    summary as summary_routes,
    tag_aliases_admin as tag_aliases_admin_routes,
    tag_colour as tag_colour_routes,
    tag_gallery as tag_gallery_routes,
    tag_merge as tag_merge_routes,
    tag_merge_wizard as tag_merge_wizard_routes,
    tag_ocr_export as tag_ocr_export_routes,
    tag_tree as tag_tree_routes,
    tag_trends as tag_trends_routes,
    tags as tags_routes,
    theme as theme_routes,
    topics as topics_routes,
    thumb_dedup as thumb_dedup_routes,
    thumb_regen as thumb_regen_routes,
    thumbnails as thumbnails_routes,
    time_on_app as time_on_app_routes,
    timeline,
    timeline_api as timeline_api_routes,
    timesheet as timesheet_routes,
    top100 as top100_routes,
    vault as vault_routes,
    visual_diff as visual_diff_routes,
    webhooks_routes,
    whats_new as whats_new_routes,
    word_search as word_search_routes,
    words_csv as words_csv_routes,
    weekly_digests as weekly_digests_routes,
    weekly_pdf as weekly_pdf_routes,
    weekly_stats_card as weekly_stats_card_routes,
    whitelist,
    budget_status as budget_status_routes,
    mic_toggle as mic_toggle_routes,
    memory as memory_routes,
    power_mode as power_mode_routes,
    capture_settings as capture_settings_routes,
    activity_heatmap as activity_heatmap_routes,
    audio_player as audio_player_routes,
    card_enrichment_settings as card_enrichment_settings_routes,
    per_app_digest_pdf as per_app_digest_pdf_routes,
    tag_rule_stats as tag_rule_stats_routes,
    clipboard_semantic as clipboard_semantic_routes,
    weekly_cards as weekly_cards_routes,
    meeting_pause as meeting_pause_routes,
    llm_cost as llm_cost_routes,
    shortcuts_help as shortcuts_help_routes,
    shot_annotations as shot_annotations_routes,
    opml_export as opml_export_routes,
    quality_lab as quality_lab_routes,
    capture_blocklist_admin as capture_blocklist_admin_routes,
    auto_translate_settings as auto_translate_settings_routes,
    timeline_preview as timeline_preview_routes,
    multi_monitor as multi_monitor_routes,
    qa_stream as qa_stream_routes,
    settings_hub as settings_hub_routes,
    shot_alt_text_settings as shot_alt_text_settings_routes,
    auto_pin_admin as auto_pin_admin_routes,
    today_vs_average as today_vs_average_routes,
    pinned_feed as pinned_feed_routes,
    ocr_confidence_chart as ocr_confidence_chart_routes,
    voice_note as voice_note_routes,
    voice_note_widget as voice_note_widget_routes,
    voice as voice_routes,
    advanced_settings as advanced_settings_routes,
    entities as entities_routes,
    shot_compare as shot_compare_routes,
    outbox_admin as outbox_admin_routes,
    focus_ics_export as focus_ics_export_routes,
    obsidian_settings as obsidian_settings_routes,
    dashboard_widget_editor as dashboard_widget_editor_routes,
    daily_pin_enrichment_settings as daily_pin_enrichment_settings_routes,
    jump_to as jump_to_routes,
    redaction_packs as redaction_packs_routes,
    metrics_export as metrics_export_routes,
    shot_reactions as shot_reactions_routes,
    long_reads as long_reads_routes,
    privacy_mode_admin as privacy_mode_admin_routes,
    s3_sync_settings as s3_sync_settings_routes,
    dashboard_card_png as dashboard_card_png_routes,
    privacy_bundles_admin as privacy_bundles_admin_routes,
    pareto as pareto_routes,
    tag_feed as tag_feed_routes,
    notifications as notifications_routes,
    tour as tour_routes,
    day_markdown_export as day_markdown_export_routes,
    weekly_rollup_settings as weekly_rollup_settings_routes,
    active_window_timeline as active_window_timeline_routes,
    shot_annotation_autosave as shot_annotation_autosave_routes,
    hotkey_settings as hotkey_settings_routes,
    ocr_rerun as ocr_rerun_routes,
    timeline_filters as timeline_filters_routes,
    rate_advisor as rate_advisor_routes,
    journal_voice as journal_voice_routes,
    tag_stats as tag_stats_routes,
    redaction_preview as redaction_preview_routes,
    capture_sessions as capture_sessions_routes,
    weekly_highlights as weekly_highlights_routes,
    dup_finder as dup_finder_routes,
    demo_seeder as demo_seeder_routes,
    app_budgets as app_budgets_routes,
    insight_cards as insight_cards_routes,
    day_pdf_export as day_pdf_export_routes,
    hashtag_suggest as hashtag_suggest_routes,
    csv_export as csv_export_routes,
    i18n_de_check as i18n_de_check_routes,
    palette_commands as palette_commands_routes,
    palette_command_admin as palette_command_admin_routes,
    chrono_parse as chrono_parse_routes,
    ai_reminders as ai_reminders_routes,
    timeline_log as timeline_log_routes,
    monthly_comparison as monthly_comparison_routes,
    focus_whitelist as focus_whitelist_routes,
    shot_privacy_masks as shot_privacy_masks_routes,
    yearly_wrapped as yearly_wrapped_routes,
    ai_reminders_ics as ai_reminders_ics_routes,
    audit_log_rotation as audit_log_rotation_routes,
    sketch_notes as sketch_notes_routes,
    voice_search as voice_search_routes,
    focus_profiles as focus_profiles_routes,
    tag_autocomplete as tag_autocomplete_routes,
    capture_quality as capture_quality_routes,
    changelog as changelog_routes,
    mobile_bottom_nav as mobile_bottom_nav_routes,
    annotation_diff as annotation_diff_routes,
    url_time as url_time_routes,
    smart_dedup as smart_dedup_routes,
    pinboard as pinboard_routes,
    bulk_tag as bulk_tag_routes,
    this_day_replay as this_day_replay_routes,
    now_dashboard as now_dashboard_routes,
    email_weekly_digest_settings as email_weekly_digest_settings_routes,
    memory_of_day_settings as memory_of_day_settings_routes,
    health_dashboard as health_dashboard_routes,
    heartbeat_alerts as heartbeat_alerts_routes,
    db_integrity as db_integrity_routes,
    quick_actions as quick_actions_routes,
    api_tokens_admin as api_tokens_admin_routes,
    audio_waveform as audio_waveform_routes,
    tag_canonicaliser as tag_canonicaliser_routes,
    app_icons as app_icons_routes,
    stale_note_pruner as stale_note_pruner_routes,
    smart_pin as smart_pin_routes,
    tag_email_digest as tag_email_digest_routes,
    changelog_rss as changelog_rss_routes,
    app_summary_card as app_summary_card_routes,
    webhook_csv_pipeline as webhook_csv_pipeline_routes,
    sleep_mode as sleep_mode_routes,
    code_shots as code_shots_routes,
    workspaces as workspaces_routes,
    metrics_extended as metrics_extended_routes,
    help_walkthrough as help_walkthrough_routes,
    landing as landing_routes,
    blog as blog_routes,
    memory_graph as memory_graph_routes,
    auth as auth_routes,
    devices as devices_routes,
    sync_api as sync_api_routes,
    notes_sync as notes_sync_routes,
    chat_sessions as chat_sessions_routes,
    storage_admin as storage_admin_routes,
    ios_ingest as ios_ingest_routes,
    install as install_routes,
    llm_models as llm_models_routes,
    dataset_admin as dataset_admin_routes,
    mcp_admin as mcp_admin_routes,
    workspace_admin as workspace_admin_routes,
)
from app.workers import (
    get_controller,
    run_audio_retention_worker,
    run_audio_worker,
    run_auto_backup_scheduler,
    run_capture_loop,
    run_clipboard_worker,
    run_daily_email_scheduler,
    run_day_end_summary_scheduler,
    run_saved_search_alert_worker,
    run_webhook_retry_worker,
    run_weekly_stats_email_scheduler,
    run_inbox_worker,
    run_monthly_digest_scheduler,
    run_digest_scheduler,
    run_embeddings_worker,
    run_ocr_worker,
    run_retention_worker,
    run_weekly_digest_scheduler,
)

log = get_logger("persona.web")

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    """Build the FastAPI application instance."""
    configure_logging()
    settings = get_settings()
    settings.ensure_directories()

    middleware = [
        Middleware(SetupGateMiddleware),
        # T5 (2026-06-07) — auth gate sits BEFORE the API auth middleware
        # so /landing + /auth/* are reachable without any cookie, and
        # browser requests for protected pages bounce to /landing as a
        # 303 instead of a JSON 401.
        Middleware(AuthGateMiddleware),
        Middleware(ApiAuthMiddleware),
        Middleware(GZipMiddleware, minimum_size=512),
        Middleware(
            CORSMiddleware,
            allow_origins=["chrome-extension://*", "moz-extension://*"],
            allow_origin_regex=r"^(chrome|moz)-extension://.*$",
            allow_methods=["POST", "GET", "OPTIONS"],
            allow_headers=["Content-Type"],
            max_age=86400,
        ),
    ]

    app = FastAPI(
        title="Persona",
        version="1.66.0",
        description="Open-source personal AI memory.",
        lifespan=_lifespan,
        middleware=middleware,
    )

    if STATIC_DIR.exists():
        # Serve /static/sw.js with no-cache headers so a CACHE_VERSION
        # bump propagates to browsers within minutes instead of the
        # browser-default ~24h sw.js stale window. The rest of /static/*
        # keeps the default StaticFiles caching.
        @app.get("/static/sw.js")
        async def serve_service_worker() -> FileResponse:  # noqa: D401
            sw_path = STATIC_DIR / "sw.js"
            return FileResponse(
                sw_path,
                media_type="application/javascript",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                    "Service-Worker-Allowed": "/",
                },
            )

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(timeline.router)
    app.include_router(search_routes.router)
    app.include_router(screenshot.router)
    app.include_router(settings_routes.router)
    app.include_router(stats.router)
    app.include_router(capture_api.router)
    app.include_router(thumbnails_routes.router)
    app.include_router(whitelist.router)
    app.include_router(export.router)
    app.include_router(summary_routes.router)
    app.include_router(health.router)
    # csv_export is registered later under the csv_export_routes alias —
    # registering both here AND there duplicated every /export/*.csv route.
    app.include_router(calendar_routes.router)
    app.include_router(tags_routes.router)
    app.include_router(analysis_routes.router)
    app.include_router(notes_routes.router)
    app.include_router(ocr_status.router)
    app.include_router(embeddings_status.router)
    app.include_router(journal_routes.router)
    app.include_router(help_routes.router)
    app.include_router(bulk_routes.router)
    app.include_router(budget_routes.router)
    app.include_router(pin_routes.router)
    app.include_router(qa_routes.router)
    app.include_router(archive_routes.router)
    app.include_router(app_stats_routes.router)
    app.include_router(digest_routes.router)
    app.include_router(full_export_routes.router)
    app.include_router(timeline_api_routes.router)
    app.include_router(icons_routes.router)
    app.include_router(topics_routes.router)
    app.include_router(daily_digests_routes.router)
    app.include_router(rss_routes.router)
    app.include_router(share_routes.router)
    app.include_router(timesheet_routes.router)
    app.include_router(mobile_routes.router)
    app.include_router(webhooks_routes.router)
    app.include_router(companion_routes.router)
    app.include_router(focus_routes.router)
    app.include_router(reminders_routes.router)
    app.include_router(reading_routes.router)
    app.include_router(vault_routes.router)
    app.include_router(note_assist_routes.router)
    app.include_router(auto_tag_routes.router)
    app.include_router(process_remap_routes.router)
    app.include_router(journal_export_routes.router)
    app.include_router(about_routes.router)
    app.include_router(range_timeline_routes.router)
    app.include_router(app_overrides_routes.router)
    app.include_router(diff_picker_routes.router)
    app.include_router(quiet_hours_routes.router)
    app.include_router(share_collection_routes.router)
    app.include_router(ocr_admin_routes.router)
    app.include_router(archive_browse_routes.router)
    app.include_router(regex_rules_routes.router)
    app.include_router(doctor_routes.router)
    app.include_router(weekly_digests_routes.router)
    app.include_router(auto_collections_routes.router)
    app.include_router(ocr_skip_routes.router)
    app.include_router(redaction_routes.router)
    app.include_router(storage_report_routes.router)
    app.include_router(note_templates_routes.router)
    app.include_router(notes_search_routes.router)
    app.include_router(annotations_routes.router)
    app.include_router(saved_searches_routes.router)
    app.include_router(streak_routes.router)
    app.include_router(heatmap_routes.router)
    app.include_router(keywords_routes.router)
    app.include_router(shot_of_day_routes.router)
    app.include_router(time_on_app_routes.router)
    app.include_router(ocr_languages_routes.router)
    app.include_router(favourites_routes.router)
    app.include_router(bulk_delete_routes.router)
    app.include_router(hour_histogram_routes.router)
    app.include_router(idle_stats_routes.router)
    app.include_router(ocr_phrase_tags_routes.router)
    app.include_router(smtp_settings_routes.router)
    app.include_router(pdf_export_routes.router)
    app.include_router(theme_routes.router)
    app.include_router(tag_trends_routes.router)
    app.include_router(diff_slider_routes.router)
    app.include_router(weekly_pdf_routes.router)
    app.include_router(ocr_diff_routes.router)
    app.include_router(api_tokens_routes.router)
    app.include_router(clipboard_routes.router)
    app.include_router(ocr_overlay_routes.router)
    app.include_router(ics_export_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(day_tldr_routes.router)
    app.include_router(settings_backup_routes.router)
    app.include_router(health_dashboard_routes.router)
    app.include_router(inbox_routes.router)
    app.include_router(palette_routes.router)
    app.include_router(shot_of_week_routes.router)
    app.include_router(stats_csv_routes.router)
    app.include_router(ocr_language_stats_routes.router)
    app.include_router(archive_bundle_routes.router)
    app.include_router(live_sse_routes.router)
    app.include_router(ocr_txt_export_routes.router)
    app.include_router(recycle_routes.router)
    app.include_router(search_facets_routes.router)
    app.include_router(drag_to_tag_routes.router)
    app.include_router(bookmarklet_routes.router)
    app.include_router(day_scrubber_routes.router)
    app.include_router(ocr_retry_routes.router)
    app.include_router(day_collage_routes.router)
    app.include_router(day_overview_page_routes.router)
    app.include_router(analytics_page_routes.router)
    app.include_router(shot_share_routes.router)
    app.include_router(shot_share_ui_routes.router)
    app.include_router(ocr_near_dup_routes.router)
    app.include_router(public_day_routes.router)
    app.include_router(app_icons_routes.router)
    app.include_router(encrypted_notes_routes.router)
    app.include_router(retention_preview_routes.router)
    # tag_colour is nested inside tags_routes (app/web/routes/tags.py uses
    # router.include_router(tag_colour_router)) — registering here too duped routes.
    app.include_router(day_kanban_routes.router)
    app.include_router(notes_timeline_routes.router)
    app.include_router(dup_suggest_routes.router)
    app.include_router(audit_rss_routes.router)
    app.include_router(permalinks_routes.router)
    app.include_router(reading_time_routes.router)
    app.include_router(tag_merge_routes.router)
    app.include_router(visual_diff_routes.router)
    app.include_router(app_retention_routes.router)
    app.include_router(feature_index_routes.router)
    app.include_router(query_api_routes.router)
    app.include_router(setup_routes.router)
    app.include_router(shot_dimensions_routes.router)
    app.include_router(reading_mode_routes.router)
    app.include_router(thumb_dedup_routes.router)
    app.include_router(qr_routes.router)
    app.include_router(storage_savings_routes.router)
    app.include_router(ocr_vision_routes.router)
    app.include_router(digest_prompts_routes.router)
    app.include_router(system_prompt_routes.router)
    app.include_router(mac_fs_routes.router)
    app.include_router(profile_routes.router)
    app.include_router(memory_settings_routes.router)
    app.include_router(privacy_settings_routes.router)
    app.include_router(briefing_routes.router)
    app.include_router(integrations_settings_routes.router)
    app.include_router(skills_settings_routes.router)
    app.include_router(voice_chat_routes.router)
    app.include_router(alice_routes.router)
    app.include_router(root_control_routes.router)
    app.include_router(activity_page_routes.router)
    app.include_router(shot_embed_routes.router)
    app.include_router(diag_bundle_routes.router)
    app.include_router(embeddings_reindex_routes.router)
    app.include_router(per_app_digest_routes.router)
    app.include_router(query_collections_routes.router)
    app.include_router(app_icons_admin_routes.router)
    app.include_router(pinmap_routes.router)
    app.include_router(cal_nav_routes.router)
    app.include_router(sitemap_routes.router)
    app.include_router(app_aliases_routes.router)
    app.include_router(idle_week_routes.router)
    app.include_router(shot_token_cloud_routes.router)
    app.include_router(share_visits_csv_routes.router)
    app.include_router(digest_card_routes.router)
    app.include_router(random_shot_routes.router)
    app.include_router(lang_autodetect_routes.router)
    app.include_router(bulk_pin_routes.router)
    app.include_router(ocr_error_rate_routes.router)
    app.include_router(app_groups_routes.router)
    app.include_router(sticky_notes_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(ocr_length_chart_routes.router)
    app.include_router(search_autocomplete_routes.router)
    app.include_router(sticky_export_routes.router)
    app.include_router(push_notif_routes.router)
    app.include_router(app_capture_skip_routes.router)
    app.include_router(semantic_similar_routes.router)
    app.include_router(monthly_digests_routes.router)
    app.include_router(app_health_routes.router)
    app.include_router(ocr_rerun_n_routes.router)
    app.include_router(multi_shot_zip_routes.router)
    app.include_router(monthly_digest_card_routes.router)
    app.include_router(shot_lock_routes.router)
    app.include_router(rss_index_routes.router)
    app.include_router(llm_switcher_routes.router)
    app.include_router(multi_day_diff_routes.router)
    app.include_router(screenshot_frame_routes.router)
    app.include_router(settings_diff_routes.router)
    app.include_router(bulk_collection_add_routes.router)
    app.include_router(external_ping_routes.router)
    app.include_router(ping_heatmap_routes.router)
    app.include_router(ocr_vision_replace_routes.router)
    app.include_router(annotations_ndjson_routes.router)
    app.include_router(audit_timeline_routes.router)
    app.include_router(notes_link_checker_routes.router)
    app.include_router(share_analytics_routes.router)
    app.include_router(ocr_find_replace_routes.router)
    app.include_router(day_ocr_diff_routes.router)
    app.include_router(monthly_stats_csv_routes.router)
    app.include_router(kanban_csv_routes.router)
    app.include_router(rotate_gallery_routes.router)
    app.include_router(share_collection_pdf_routes.router)
    app.include_router(embeddings_stats_routes.router)
    app.include_router(dashboard_tiles_routes.router)
    app.include_router(ocr_translate_routes.router)
    app.include_router(stickers_gallery_routes.router)
    app.include_router(share_collection_zip_routes.router)
    app.include_router(slack_summary_routes.router)
    app.include_router(thumb_regen_routes.router)
    app.include_router(focus_blocklist_routes.router)
    app.include_router(feed_tokens_routes.router)
    app.include_router(screenshot_crop_routes.router)
    app.include_router(settings_api_routes.router)
    app.include_router(phrase_frequency_routes.router)
    app.include_router(dashboard_widgets_routes.router)
    app.include_router(ocr_emails_routes.router)
    app.include_router(ocr_phones_routes.router)
    app.include_router(search_tag_all_routes.router)
    app.include_router(retention_trend_routes.router)
    app.include_router(weekly_stats_card_routes.router)
    app.include_router(budget_status_routes.router)
    app.include_router(mic_toggle_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(power_mode_routes.router)
    app.include_router(capture_settings_routes.router)
    app.include_router(activity_heatmap_routes.router)
    app.include_router(audio_player_routes.router)
    app.include_router(card_enrichment_settings_routes.router)
    app.include_router(per_app_digest_pdf_routes.router)
    app.include_router(tag_rule_stats_routes.router)
    app.include_router(clipboard_semantic_routes.router)
    app.include_router(weekly_cards_routes.router)
    app.include_router(meeting_pause_routes.router)
    app.include_router(llm_cost_routes.router)
    app.include_router(shortcuts_help_routes.router)
    app.include_router(shot_annotations_routes.router)
    app.include_router(opml_export_routes.router)
    app.include_router(quality_lab_routes.router)
    app.include_router(capture_blocklist_admin_routes.router)
    app.include_router(auto_translate_settings_routes.router)
    app.include_router(timeline_preview_routes.router)
    app.include_router(multi_monitor_routes.router)
    # qa_stream is nested inside qa_routes (app/web/routes/qa.py uses
    # router.include_router(qa_stream_router)) — registering here too duped routes.
    app.include_router(settings_hub_routes.router)
    # Phase 2 — браузер-агент + MCP-рантайм переключатель (/settings/automation).
    from app.web.routes import automation_settings as automation_settings_routes  # noqa: PLC0415
    app.include_router(automation_settings_routes.router)
    app.include_router(shot_alt_text_settings_routes.router)
    app.include_router(auto_pin_admin_routes.router)
    app.include_router(today_vs_average_routes.router)
    app.include_router(pinned_feed_routes.router)
    app.include_router(ocr_confidence_chart_routes.router)
    app.include_router(voice_note_routes.router)
    app.include_router(voice_note_widget_routes.router)
    app.include_router(voice_routes.router)
    app.include_router(advanced_settings_routes.router)
    app.include_router(entities_routes.router)
    app.include_router(shot_compare_routes.router)
    app.include_router(outbox_admin_routes.router)
    app.include_router(focus_ics_export_routes.router)
    app.include_router(obsidian_settings_routes.router)
    app.include_router(dashboard_widget_editor_routes.router)
    app.include_router(daily_pin_enrichment_settings_routes.router)
    app.include_router(jump_to_routes.router)
    app.include_router(redaction_packs_routes.router)
    app.include_router(metrics_export_routes.router)
    app.include_router(shot_reactions_routes.router)
    app.include_router(long_reads_routes.router)
    app.include_router(privacy_mode_admin_routes.router)
    app.include_router(s3_sync_settings_routes.router)
    app.include_router(dashboard_card_png_routes.router)
    app.include_router(privacy_bundles_admin_routes.router)
    app.include_router(pareto_routes.router)
    app.include_router(tag_feed_routes.router)
    app.include_router(notifications_routes.router)
    app.include_router(tour_routes.router)
    app.include_router(day_markdown_export_routes.router)
    app.include_router(weekly_rollup_settings_routes.router)
    app.include_router(active_window_timeline_routes.router)
    app.include_router(shot_annotation_autosave_routes.router)
    app.include_router(hotkey_settings_routes.router)
    app.include_router(ocr_rerun_routes.router)
    app.include_router(timeline_filters_routes.router)
    app.include_router(rate_advisor_routes.router)
    app.include_router(journal_voice_routes.router)
    app.include_router(tag_stats_routes.router)
    app.include_router(redaction_preview_routes.router)
    app.include_router(capture_sessions_routes.router)
    app.include_router(weekly_highlights_routes.router)
    app.include_router(dup_finder_routes.router)
    app.include_router(demo_seeder_routes.router)
    app.include_router(app_budgets_routes.router)
    app.include_router(insight_cards_routes.router)
    # day_pdf_export is nested inside day_markdown_export_routes — registering
    # it again here duped /export/day/{day}.pdf + /day/{day}/pdf-preview.
    app.include_router(hashtag_suggest_routes.router)
    app.include_router(csv_export_routes.router)
    app.include_router(i18n_de_check_routes.router)
    app.include_router(palette_commands_routes.router)
    app.include_router(palette_command_admin_routes.router)
    # chrono_parse is nested inside qa_routes (qa.py uses
    # router.include_router(chrono_parse_router)) — registering here duped.
    app.include_router(ai_reminders_routes.router)
    app.include_router(timeline_log_routes.router)
    app.include_router(monthly_comparison_routes.router)
    app.include_router(focus_whitelist_routes.router)
    app.include_router(shot_privacy_masks_routes.router)
    app.include_router(yearly_wrapped_routes.router)
    app.include_router(ai_reminders_ics_routes.router)
    app.include_router(audit_log_rotation_routes.router)
    app.include_router(sketch_notes_routes.router)
    app.include_router(voice_search_routes.router)
    app.include_router(focus_profiles_routes.router)
    app.include_router(tag_autocomplete_routes.router)
    app.include_router(capture_quality_routes.router)
    app.include_router(changelog_routes.router)
    app.include_router(mobile_bottom_nav_routes.router)
    app.include_router(annotation_diff_routes.router)
    app.include_router(url_time_routes.router)
    app.include_router(smart_dedup_routes.router)
    app.include_router(pinboard_routes.router)
    app.include_router(bulk_tag_routes.router)
    app.include_router(this_day_replay_routes.router)
    app.include_router(now_dashboard_routes.router)
    app.include_router(email_weekly_digest_settings_routes.router)
    app.include_router(memory_of_day_settings_routes.router)
    app.include_router(heartbeat_alerts_routes.router)
    app.include_router(db_integrity_routes.router)
    app.include_router(quick_actions_routes.router)
    app.include_router(api_tokens_admin_routes.router)
    app.include_router(audio_waveform_routes.router)
    app.include_router(tag_canonicaliser_routes.router)
    app.include_router(stale_note_pruner_routes.router)
    app.include_router(smart_pin_routes.router)
    app.include_router(tag_email_digest_routes.router)
    app.include_router(changelog_rss_routes.router)
    app.include_router(app_summary_card_routes.router)
    app.include_router(webhook_csv_pipeline_routes.router)
    app.include_router(sleep_mode_routes.router)
    app.include_router(code_shots_routes.router)
    app.include_router(workspaces_routes.router)
    app.include_router(metrics_extended_routes.router)
    app.include_router(help_walkthrough_routes.router)
    app.include_router(landing_routes.router)
    app.include_router(blog_routes.router)
    app.include_router(memory_graph_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(devices_routes.router)
    app.include_router(sync_api_routes.router)
    app.include_router(notes_sync_routes.router)
    app.include_router(chat_sessions_routes.router)
    app.include_router(storage_admin_routes.router)
    app.include_router(ios_ingest_routes.router)
    app.include_router(install_routes.router)
    app.include_router(llm_models_routes.router)
    app.include_router(dataset_admin_routes.router)
    app.include_router(mcp_admin_routes.router)
    app.include_router(workspace_admin_routes.router)
    app.include_router(sticky_search_routes.router)
    app.include_router(audit_replay_routes.router)
    app.include_router(tag_gallery_routes.router)
    app.include_router(app_calendar_routes.router)
    app.include_router(ocr_history_routes.router)
    app.include_router(tag_ocr_export_routes.router)
    app.include_router(day_json_routes.router)
    app.include_router(words_csv_routes.router)
    app.include_router(import_screenshot_routes.router)
    app.include_router(ocr_edit_routes.router)
    app.include_router(facet_sets_routes.router)
    app.include_router(top100_routes.router)
    app.include_router(tag_merge_wizard_routes.router)
    app.include_router(corpus_search_routes.router)
    app.include_router(sparkline_svg_routes.router)
    app.include_router(dedup_cluster_routes.router)
    app.include_router(bulk_untag_routes.router)
    app.include_router(llm_usage_routes.router)
    app.include_router(shot_groups_routes.router)
    app.include_router(phrase_autotag_suggest_routes.router)
    app.include_router(whats_new_routes.router)
    app.include_router(note_attachments_routes.router)
    app.include_router(annotations_csv_routes.router)
    app.include_router(kbd_shortcuts_routes.router)
    app.include_router(tag_aliases_admin_routes.router)
    app.include_router(search_query_stats_routes.router)
    app.include_router(side_by_side_routes.router)
    app.include_router(word_search_routes.router)
    app.include_router(bulk_favourite_routes.router)
    app.include_router(personal_metrics_routes.router)
    app.include_router(tag_tree_routes.router)
    app.include_router(sentiment_stats_routes.router)
    app.include_router(app_shots_csv_routes.router)
    app.include_router(collection_visit_stats_routes.router)
    app.include_router(shot_summary_routes.router)
    app.include_router(shot_colours_routes.router)
    app.include_router(notes_csv_import_routes.router)
    app.include_router(capture_weekly_trend_routes.router)
    app.include_router(audio_day_routes.router)
    app.include_router(audio_segment_routes.router)
    app.include_router(audio_settings_routes.router)
    app.include_router(audio_search_routes.router)
    app.include_router(audio_stats_routes.router)
    app.include_router(agent_api_routes.router)
    app.include_router(agents_admin_routes.router)

    return app


async def _run_hourly_card_worker(controller: object) -> None:
    """Adapter so hourly_card_worker plugs into the lifespan task list.

    The worker doesn't need the CaptureController (it reads from DB,
    doesn't capture anything), but the lifespan list pattern is
    ``(controller) -> coroutine``. We accept and ignore.
    """
    from app.workers.hourly_card_worker import run_hourly_card_worker  # noqa: PLC0415

    await run_hourly_card_worker()


async def _run_daily_pin_worker(controller: object) -> None:
    """Same adapter pattern as hourly card worker — tier 5 daily pin."""
    from app.workers.daily_pin_worker import run_daily_pin_worker  # noqa: PLC0415

    await run_daily_pin_worker()


async def _run_card_enrichment_worker(controller: object) -> None:
    """Adapter for the v1.19 LLM-enrichment-of-hourly-cards worker."""
    from app.workers.card_enrichment_worker import run_card_enrichment_worker  # noqa: PLC0415

    await run_card_enrichment_worker()


async def _run_tag_rule_worker(controller: object) -> None:
    """Adapter for the v1.20 auto-tag-rule applier worker."""
    from app.workers.tag_rule_worker import run_tag_rule_worker  # noqa: PLC0415

    await run_tag_rule_worker()


async def _run_weekly_card_worker(controller: object) -> None:
    """Adapter for the v1.21 tier-2 weekly summary card worker."""
    from app.workers.weekly_card_worker import run_weekly_card_worker  # noqa: PLC0415

    await run_weekly_card_worker()


async def _run_auto_translate_worker(controller: object) -> None:
    """Adapter for the v1.23 voice-segment auto-translate worker."""
    from app.workers.auto_translate_worker import run_auto_translate_worker  # noqa: PLC0415

    await run_auto_translate_worker()


async def _run_alt_text_worker(controller: object) -> None:
    """Adapter for the v1.35 per-shot LLM alt-text worker."""
    from app.workers.alt_text_worker import run_alt_text_worker  # noqa: PLC0415

    await run_alt_text_worker()


async def _run_auto_pin_worker(controller: object) -> None:
    """Adapter for the v1.35 regex-rule auto-pin worker."""
    from app.workers.auto_pin_worker import run_auto_pin_worker  # noqa: PLC0415

    await run_auto_pin_worker()


async def _run_entity_extractor_worker(controller: object) -> None:
    """Adapter for the v1.37 cross-day entity extraction worker."""
    from app.workers.entity_extractor_worker import run_entity_extractor_worker  # noqa: PLC0415

    await run_entity_extractor_worker()


async def _run_obsidian_sync_worker(controller: object) -> None:
    """Adapter for the v1.38 Obsidian vault sync worker."""
    from app.workers.obsidian_sync_worker import run_obsidian_sync_worker  # noqa: PLC0415

    await run_obsidian_sync_worker()


async def _run_daily_pin_enrichment_worker(controller: object) -> None:
    """Adapter for the v1.40 daily_pin LLM enrichment worker."""
    from app.workers.daily_pin_enrichment_worker import run_daily_pin_enrichment_worker  # noqa: PLC0415

    await run_daily_pin_enrichment_worker()


async def _run_long_read_worker(controller: object) -> None:
    """Adapter for the v1.41 long-read auto-detection worker."""
    from app.workers.long_read_worker import run_long_read_worker  # noqa: PLC0415

    await run_long_read_worker()


async def _run_s3_sync_worker(controller: object) -> None:
    """Adapter for the v1.42 nightly S3-compatible sync worker."""
    from app.workers.s3_sync_worker import run_s3_sync_worker  # noqa: PLC0415

    await run_s3_sync_worker()


async def _run_weekly_rollup_worker(controller: object) -> None:
    """Adapter for the v1.45 LLM weekly-rollup worker."""
    from app.workers.weekly_rollup_worker import run_weekly_rollup_worker  # noqa: PLC0415

    await run_weekly_rollup_worker()


async def _run_audio_merge_worker(controller: object) -> None:
    """Adapter for the v1.46 sub-second audio-segment merge worker."""
    from app.workers.audio_merge_worker import run_audio_merge_worker  # noqa: PLC0415

    await run_audio_merge_worker()


async def _run_capture_session_worker(controller: object) -> None:
    """Adapter for the v1.48 capture-session auto-detector worker."""
    from app.workers.capture_session_worker import run_capture_session_worker  # noqa: PLC0415

    await run_capture_session_worker()


async def _run_app_budget_worker(controller: object) -> None:
    """Adapter for the v1.50 per-app daily-minutes budget alerter."""
    from app.workers.app_budget_worker import run_app_budget_worker  # noqa: PLC0415

    await run_app_budget_worker()


async def _run_ai_reminders_worker(controller: object) -> None:
    """Adapter for the v1.53 nightly AI-suggested reminders worker."""
    from app.workers.ai_reminders_worker import run_ai_reminders_worker  # noqa: PLC0415

    await run_ai_reminders_worker()


async def _run_audit_log_rotation_worker(controller: object) -> None:
    """Adapter for the v1.55 nightly audit-log rotation + gzip archive worker."""
    from app.workers.audit_log_rotation_worker import run_audit_log_rotation_worker  # noqa: PLC0415

    await run_audit_log_rotation_worker()


async def _run_url_time_worker(controller: object) -> None:
    """Adapter for the v1.58 browser-window URL-time aggregator."""
    from app.workers.url_time_worker import run_url_time_worker  # noqa: PLC0415

    await run_url_time_worker()


async def _run_smart_dedup_worker(controller: object) -> None:
    """Adapter for the v1.58 trivial-dup smart-dedup worker."""
    from app.workers.smart_dedup_worker import run_smart_dedup_worker  # noqa: PLC0415

    await run_smart_dedup_worker()


async def _run_email_weekly_digest_worker(controller: object) -> None:
    """Adapter for the v1.60 Sunday-evening weekly digest email worker."""
    from app.workers.email_weekly_digest_worker import (  # noqa: PLC0415
        run_email_weekly_digest_worker,
    )

    await run_email_weekly_digest_worker()


async def _run_memory_of_day_worker(controller: object) -> None:
    """Adapter for the v1.60 morning memory-of-the-day push worker."""
    from app.workers.memory_of_day_worker import run_memory_of_day_worker  # noqa: PLC0415

    await run_memory_of_day_worker()


async def _run_dream_worker(controller: object) -> None:
    """Adapter for the nightly memory-reflection ("dream") worker (docs §3)."""
    from app.workers.dream_worker import run_dream_worker  # noqa: PLC0415

    await run_dream_worker()


async def _run_briefing_worker(controller: object) -> None:
    """Adapter for the proactive morning-briefing worker (MVP 7g)."""
    from app.workers.briefing_worker import run_briefing_worker  # noqa: PLC0415

    await run_briefing_worker()


async def _run_heartbeat_alert_worker(controller: object) -> None:
    """Adapter for the v1.61 worker-heartbeat gap-alert pusher."""
    from app.workers.heartbeat_alert_worker import run_heartbeat_alert_worker  # noqa: PLC0415

    await run_heartbeat_alert_worker()


async def _run_db_integrity_worker(controller: object) -> None:
    """Adapter for the v1.61 nightly DB quick-check + analyze worker."""
    from app.workers.db_integrity_worker import run_db_integrity_worker  # noqa: PLC0415

    await run_db_integrity_worker()


async def _run_audio_waveform_worker(controller: object) -> None:
    """Adapter for the v1.62 audio-segment SVG waveform backfill worker."""
    from app.workers.audio_waveform_worker import run_audio_waveform_worker  # noqa: PLC0415

    await run_audio_waveform_worker()


async def _run_smart_pin_worker(controller: object) -> None:
    """Adapter for the v1.64 morning smart-pin LLM auto-suggester."""
    from app.workers.smart_pin_worker import run_smart_pin_worker  # noqa: PLC0415

    await run_smart_pin_worker()


async def _run_tag_email_digest_worker(controller: object) -> None:
    """Adapter for the v1.64 hourly per-tag email digest dispatcher."""
    from app.workers.tag_email_digest_worker import run_tag_email_digest_worker  # noqa: PLC0415

    await run_tag_email_digest_worker()


async def _run_webhook_csv_worker(controller: object) -> None:
    """Adapter for the v1.65 hourly webhook-CSV pipeline dispatcher."""
    from app.workers.webhook_csv_worker import run_webhook_csv_worker  # noqa: PLC0415

    await run_webhook_csv_worker()


async def _run_ocr_code_detector_worker(controller: object) -> None:
    """Adapter for the v1.66 OCR-text code-detection backfill worker."""
    from app.workers.ocr_code_detector_worker import (  # noqa: PLC0415
        run_ocr_code_detector_worker,
    )

    await run_ocr_code_detector_worker()


async def _run_sync_apply_worker(controller: object) -> None:
    """Adapter for the T5 sync-event apply worker."""
    from app.workers.sync_apply_worker import run_sync_apply_worker  # noqa: PLC0415

    await run_sync_apply_worker()


async def _run_storage_cleanup_worker(controller: object) -> None:
    """T15 (2026-06-07) — nightly screenshot cleanup honouring the
    user's retention policy at /storage."""
    from app.workers.storage_cleanup_worker import run as run_storage_cleanup  # noqa: PLC0415

    await run_storage_cleanup()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise DB, start workers, and tear them down on shutdown."""
    await init_database()
    controller = get_controller()

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(run_capture_loop(controller), name="capture-loop"),
        asyncio.create_task(run_ocr_worker(controller), name="ocr-worker"),
        asyncio.create_task(run_retention_worker(controller), name="retention-worker"),
        asyncio.create_task(run_embeddings_worker(controller), name="embeddings-worker"),
        asyncio.create_task(run_digest_scheduler(controller), name="digest-scheduler"),
        asyncio.create_task(run_weekly_digest_scheduler(controller), name="weekly-digest-scheduler"),
        asyncio.create_task(run_clipboard_worker(controller), name="clipboard-worker"),
        asyncio.create_task(run_inbox_worker(controller), name="inbox-worker"),
        asyncio.create_task(run_daily_email_scheduler(controller), name="daily-email-scheduler"),
        asyncio.create_task(run_saved_search_alert_worker(controller), name="saved-search-alert"),
        asyncio.create_task(run_weekly_stats_email_scheduler(controller), name="weekly-stats-email"),
        asyncio.create_task(run_webhook_retry_worker(controller), name="webhook-retry"),
        asyncio.create_task(run_monthly_digest_scheduler(controller), name="monthly-digest-scheduler"),
        asyncio.create_task(run_day_end_summary_scheduler(controller), name="day-end-summary"),
        asyncio.create_task(run_auto_backup_scheduler(controller), name="auto-backup"),
        asyncio.create_task(run_audio_worker(controller), name="audio-worker"),
        asyncio.create_task(run_audio_retention_worker(controller), name="audio-retention"),
        asyncio.create_task(
            _run_hourly_card_worker(controller),
            name="hourly-card-worker",
        ),
        asyncio.create_task(
            _run_daily_pin_worker(controller),
            name="daily-pin-worker",
        ),
        asyncio.create_task(
            _run_card_enrichment_worker(controller),
            name="card-enrichment-worker",
        ),
        asyncio.create_task(
            _run_tag_rule_worker(controller),
            name="tag-rule-worker",
        ),
        asyncio.create_task(
            _run_weekly_card_worker(controller),
            name="weekly-card-worker",
        ),
        asyncio.create_task(
            _run_auto_translate_worker(controller),
            name="auto-translate-worker",
        ),
        asyncio.create_task(
            _run_alt_text_worker(controller),
            name="alt-text-worker",
        ),
        asyncio.create_task(
            _run_auto_pin_worker(controller),
            name="auto-pin-worker",
        ),
        asyncio.create_task(
            _run_entity_extractor_worker(controller),
            name="entity-extractor-worker",
        ),
        asyncio.create_task(
            _run_obsidian_sync_worker(controller),
            name="obsidian-sync-worker",
        ),
        asyncio.create_task(
            _run_daily_pin_enrichment_worker(controller),
            name="daily-pin-enrichment-worker",
        ),
        asyncio.create_task(
            _run_long_read_worker(controller),
            name="long-read-worker",
        ),
        asyncio.create_task(
            _run_s3_sync_worker(controller),
            name="s3-sync-worker",
        ),
        asyncio.create_task(
            _run_weekly_rollup_worker(controller),
            name="weekly-rollup-worker",
        ),
        asyncio.create_task(
            _run_audio_merge_worker(controller),
            name="audio-merge-worker",
        ),
        asyncio.create_task(
            _run_capture_session_worker(controller),
            name="capture-session-worker",
        ),
        asyncio.create_task(
            _run_app_budget_worker(controller),
            name="app-budget-worker",
        ),
        asyncio.create_task(
            _run_ai_reminders_worker(controller),
            name="ai-reminders-worker",
        ),
        asyncio.create_task(
            _run_audit_log_rotation_worker(controller),
            name="audit-log-rotation-worker",
        ),
        asyncio.create_task(
            _run_url_time_worker(controller),
            name="url-time-worker",
        ),
        asyncio.create_task(
            _run_smart_dedup_worker(controller),
            name="smart-dedup-worker",
        ),
        asyncio.create_task(
            _run_email_weekly_digest_worker(controller),
            name="email-weekly-digest-worker",
        ),
        asyncio.create_task(
            _run_memory_of_day_worker(controller),
            name="memory-of-day-worker",
        ),
        # Hermes-style nightly memory consolidation ("сон") — opt-in
        # (kv dream_enabled), fires at kv dream_hour_local (default 03:00).
        asyncio.create_task(
            _run_dream_worker(controller),
            name="dream-worker",
        ),
        asyncio.create_task(
            _run_briefing_worker(controller),
            name="briefing-worker",
        ),
        asyncio.create_task(
            _run_heartbeat_alert_worker(controller),
            name="heartbeat-alert-worker",
        ),
        asyncio.create_task(
            _run_db_integrity_worker(controller),
            name="db-integrity-worker",
        ),
        asyncio.create_task(
            _run_audio_waveform_worker(controller),
            name="audio-waveform-worker",
        ),
        asyncio.create_task(
            _run_smart_pin_worker(controller),
            name="smart-pin-worker",
        ),
        asyncio.create_task(
            _run_tag_email_digest_worker(controller),
            name="tag-email-digest-worker",
        ),
        asyncio.create_task(
            _run_webhook_csv_worker(controller),
            name="webhook-csv-worker",
        ),
        asyncio.create_task(
            _run_ocr_code_detector_worker(controller),
            name="ocr-code-detector-worker",
        ),
        # T5 (2026-06-07) — drains sync_event pending queue and
        # materialises note/kv events into the canonical tables.
        asyncio.create_task(
            _run_sync_apply_worker(controller),
            name="sync-apply-worker",
        ),
        # T15 (2026-06-07) — nightly cleanup of old screenshots per
        # ``kv.shots_retention_days``.
        asyncio.create_task(
            _run_storage_cleanup_worker(controller),
            name="storage-cleanup-worker",
        ),
    ]

    # T29 — LEAN MODE. The ~40 background workers churn SQLite and run
    # heavy/CPU ops; under load they bog the single event loop until the
    # server stops responding ("сайт зависает"). With PERSONA_LEAN_MODE=1
    # we serve pages + agent ingest only (those are request handlers, not
    # workers) and skip the workers entirely. The tasks were created above
    # but the loop hasn't run them yet (no await since creation), so
    # cancelling here means their bodies never execute. Re-enable workers
    # once each one's blocking ops are moved off the event loop.
    if _os.environ.get("PERSONA_LEAN_MODE") == "1":
        for _t in tasks:
            _t.cancel()
        tasks = []
        from app.logging_setup import get_logger as _gl  # noqa: PLC0415

        _gl("persona.web.main").warning("lifespan.lean_mode — background workers DISABLED")

    # v1.10: only pause on boot if opted-in via kv_setting capture_paused_on_boot=1
    try:
        from app.storage.db import get_connection
        from app.storage.repository import get_kv
        async with get_connection() as _kv_conn:
            _pause_flag = await get_kv(_kv_conn, "capture_paused_on_boot")
        if (_pause_flag or "0").strip() == "1":
            controller.pause()
            log.info("persona.boot.paused_per_setting")
    except Exception as exc:
        log.warning("persona.boot.pause_check_failed", error=str(exc))
    log.info("persona.started", host=get_settings().host, port=get_settings().port)

    try:
        yield
    finally:
        log.info("persona.stopping")
        controller.request_stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Phase 2 — terminate any per-session browser workers and stdio MCP
        # subprocesses so we don't leave orphaned Chromium / node processes.
        try:
            from app.browse.agent.manager import close_all as _close_browsers
            from app.mcp.runtime import shutdown_mcp_runtime as _close_mcp

            await _close_browsers()
            await _close_mcp()
        except Exception as exc:  # noqa: BLE001
            log.warning("persona.automation_shutdown_failed", error=str(exc))
        log.info("persona.stopped")


app = create_app()


def run() -> None:
    """Console-script entry point: launch uvicorn with current settings."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.web.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    run()
