ALTER TABLE reminders ADD COLUMN screenshot_id INTEGER REFERENCES screenshots(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_reminders_screenshot ON reminders(screenshot_id);
