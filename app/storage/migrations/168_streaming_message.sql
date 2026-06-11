-- T29 (2026-06-11) — incremental streaming persistence. The assistant
-- message row is created when generation starts and its content is
-- updated as tokens arrive, with is_streaming=1 while in progress. A
-- reopened tab reads the growing row (polling /live) to show the answer
-- in real time, and nothing is lost if the page is closed mid-answer.
ALTER TABLE chat_message ADD COLUMN is_streaming INTEGER NOT NULL DEFAULT 0;
