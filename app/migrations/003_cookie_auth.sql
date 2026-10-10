ALTER TABLE credential ADD COLUMN kind TEXT NOT NULL DEFAULT 'refresh_token';
UPDATE pixiv_account SET status='cookie_required', validated_at=NULL
 WHERE id IN (SELECT account_id FROM credential WHERE kind='refresh_token');
UPDATE collection_run SET cursor=NULL, state='scanning', generation=lower(hex(randomblob(16)))
 WHERE job_id IN (SELECT id FROM job WHERE kind='sync' AND state NOT IN ('succeeded', 'cancelled', 'waiting_children'));
DELETE FROM collection_item WHERE run_id IN (
 SELECT id FROM collection_run WHERE state='scanning'
);
UPDATE job SET state='waiting_auth', error_code='cookie_required', owner=NULL, lease_until=NULL,
 heartbeat=NULL, active_key=NULL
 WHERE kind IN ('sync', 'validate') AND state IN ('queued', 'running', 'waiting_auth');
DELETE FROM lease WHERE key='account:1';
