"""FastAPI application factory and entry point."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware

from app.logging_setup import configure_logging, get_logger
from app.settings import get_settings
from app.storage.db import init_database
from app.web.middleware.api_auth import ApiAuthMiddleware
from app.web.routes.setup_gate import SetupGateMiddleware
from app.web.routes import (
    about as about_routes,
    analysis as analysis_routes,
    annotations as annotations_routes,
    annotations_ndjson as annotations_ndjson_routes,
    api_tokens as api_tokens_routes,
    app_aliases as app_aliases_routes,
    app_calendar as app_calendar_routes,
    app_capture_skip as app_capture_skip_routes,
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
    audit as audit_routes,
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
    cal_nav as cal_nav_routes,
    calendar as calendar_routes,
    capture_api,
    clipboard as clipboard_routes,
    companion as companion_routes,
    corpus_search as corpus_search_routes,
    csv_export,
    daily_digests as daily_digests_routes,
    dashboard as dashboard_routes,
    dashboard_tiles as dashboard_tiles_routes,
    dashboard_widgets as dashboard_widgets_routes,
    day_collage as day_collage_routes,
    day_json as day_json_routes,
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
    note_templates as note_templates_routes,
    notes as notes_routes,
    notes_link_checker as notes_link_checker_routes,
    notes_search as notes_search_routes,
    notes_timeline as notes_timeline_routes,
    ocr_status,
    palette as palette_routes,
    pdf_export as pdf_export_routes,
    per_app_digest as per_app_digest_routes,
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
    ocr_words_tsv as ocr_words_tsv_routes,
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
    search_tag_all as search_tag_all_routes,
    sparkline_svg as sparkline_svg_routes,
    semantic_similar as semantic_similar_routes,
    share as share_routes,
    share_analytics as share_analytics_routes,
    share_visits_csv as share_visits_csv_routes,
    shot_of_day as shot_of_day_routes,
    shot_of_week as shot_of_week_routes,
    shot_share as shot_share_routes,
    shot_token_cloud as shot_token_cloud_routes,
    sitemap as sitemap_routes,
    slack_summary as slack_summary_routes,
    shot_dimensions as shot_dimensions_routes,
    shot_embed as shot_embed_routes,
    shot_groups as shot_groups_routes,
    shot_lock as shot_lock_routes,
    shot_share_ui as shot_share_ui_routes,
    share_collection as share_collection_routes,
    share_collection_pdf as share_collection_pdf_routes,
    share_collection_zip as share_collection_zip_routes,
    settings as settings_routes,
    settings_api as settings_api_routes,
    settings_backup as settings_backup_routes,
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
    tag_colour as tag_colour_routes,
    tag_gallery as tag_gallery_routes,
    tag_merge as tag_merge_routes,
    tag_merge_wizard as tag_merge_wizard_routes,
    tag_ocr_export as tag_ocr_export_routes,
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
    words_csv as words_csv_routes,
    weekly_digests as weekly_digests_routes,
    weekly_pdf as weekly_pdf_routes,
    weekly_stats_card as weekly_stats_card_routes,
    whitelist,
)
from app.workers import (
    get_controller,
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
        version="1.0.0",
        description="Open-source personal AI memory.",
        lifespan=_lifespan,
        middleware=middleware,
    )

    if STATIC_DIR.exists():
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
    app.include_router(csv_export.router)
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
    app.include_router(shot_share_routes.router)
    app.include_router(shot_share_ui_routes.router)
    app.include_router(ocr_near_dup_routes.router)
    app.include_router(public_day_routes.router)
    app.include_router(app_icons_routes.router)
    app.include_router(encrypted_notes_routes.router)
    app.include_router(retention_preview_routes.router)
    app.include_router(tag_colour_routes.router)
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
    app.include_router(sticky_search_routes.router)
    app.include_router(audit_replay_routes.router)
    app.include_router(ocr_words_tsv_routes.router)
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

    return app


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
    ]

    controller.pause()
    log.info("persona.started", host=get_settings().host, port=get_settings().port)

    try:
        yield
    finally:
        log.info("persona.stopping")
        controller.request_stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
