-- T24 (2026-06-08) — chat customisation: per-session custom system
-- prompt, auto-switch on image, MCP server registry.

ALTER TABLE chat_session ADD COLUMN custom_system_prompt TEXT;
ALTER TABLE chat_session ADD COLUMN auto_switch_on_image INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS mcp_server (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    description    TEXT,
    command        TEXT NOT NULL,                -- launch cmd (e.g. "npx -y @modelcontextprotocol/server-filesystem D:\\")
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Default placeholder servers — user enables and edits the command via UI.
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled)
VALUES
  ('filesystem', 'Read and write files on a chosen drive (e.g. D:\\Projects)',
   'npx -y @modelcontextprotocol/server-filesystem D:\\Projects', 0),
  ('shell',      'Execute shell commands (DANGEROUS — only for trusted use)',
   'npx -y @modelcontextprotocol/server-shell', 0),
  ('git',        'Read git repository status, log, diffs',
   'npx -y @modelcontextprotocol/server-git --repository .', 0),
  ('memory',     'Persistent memory across conversations (knowledge graph)',
   'npx -y @modelcontextprotocol/server-memory', 0),
  ('brave-search', 'Web search via Brave API (needs BRAVE_API_KEY)',
   'npx -y @modelcontextprotocol/server-brave-search', 0);
