-- Repair legacy sessions whose user row was removed before FK enforcement.
--
-- New writes already run with PRAGMA foreign_keys=ON. This one-time cleanup
-- restores a clean foreign_key_check for databases carrying older deleted
-- accounts; no active owner's session is affected.
DELETE FROM auth_session
 WHERE NOT EXISTS (
    SELECT 1 FROM users WHERE users.id=auth_session.user_id
 );
