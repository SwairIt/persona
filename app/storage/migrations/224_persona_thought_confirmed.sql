-- Task 5 (owner mandate 2026-07-30): the diary's "confirm into memory" button
-- needs to record, on the thought row itself, that a conclusion was already
-- promoted -- otherwise the same conclusion could be turned into a
-- remembered fact twice. Written ONLY by the web route layer
-- (app/web/routes/thinking.py) via ThoughtStore.mark_confirmed; app/thinking
-- itself never sets this column -- see tests/test_thinking_no_memory_writes.py.
ALTER TABLE persona_thought
    ADD COLUMN confirmed_at TEXT;
