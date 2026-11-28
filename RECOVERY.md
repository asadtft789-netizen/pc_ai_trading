# Recovery
Before a migration or restoration pause new exposure through the verified control and preserve supervision of every existing position. Confirm the actual database paths and ledger ownership. Investigate uncertain requests using their original client order ID and independently confirmed fills. A timeout or missing lookup is insufficient evidence for replay.

Create a unique destination outside the project checkout for each database. Run python sqlite_snapshot.py backup SOURCE NEW_SNAPSHOT for the operational store and its bound learning store. Record the time, revision, schema, store identity and receipt sequence for each. These snapshots are individually consistent; they occur at different instants. Keep the pair and verify watermark agreement.

Run python sqlite_snapshot.py restore SNAPSHOT NEW_PATH into isolated files. Verify integrity, receipt ownership, snapshots and all post-backup broker events before cutover. Keep old files. Compare confirmed events with the account and quarantine unexplained differences. Do not roll the ledger back over newer fills. Linux release restore time, hourly scheduling, encrypted off-host retention and outage alert delivery are outstanding.

## Backup sets and restore drill

The hourly timer calls `python -m backup_sets backup DATABASE BACKUP_ROOT STATUS_FILE` using the release revision from `PHOENIX_REVISION`. Each pair uses the SQLite online backup API, checks integrity and foreign keys, records SHA-256 checksums and verifies an isolated restore before publication. The learning copy follows the operational copy. Binding identities must match and the learning receipt sequence must cover the operational checkpoint. The files are separate snapshots. Later sidecar receipts can be replayed through the existing recovery contract.

Recover a local set into a new, private directory with `python -m backup_sets restore SNAPSHOT NEW_DIRECTORY`. With a configured encrypted rclone remote and separately stored recovery key, use `python -m backup_sets restore-remote SNAPSHOT_NAME NEW_DIRECTORY`. The uploader downloads for comparison and publishes a verification marker after the check. Local retention keeps approximately 25 hours of hourly sets plus the latest daily sets across seven observed days and the latest weekly sets across four observed ISO weeks. After an observed remote restore and policy review, run `python -m backup_sets prune-remote` in the dedicated remote prefix. This intentionally deletes old, complete remote sets. Apply the policy from an authorized maintenance path. Keep access to the backup decryption material separate from the backup destination.

## Incident and rollback sequence

1. Record UTC detection time, release SHA, last successful reconciliation and learning receipt, service state, pending order uncertainty, dashboard status, backup age and a sanitized journal excerpt.
2. Preserve observation and any independently established position protection. For reconciliation or ownership incidents, record the original client order identities in the restricted incident record and halt entry eligibility. Never infer an order outcome from a timeout.
3. On disk or database incidents, preserve the original files. Restore a verified pair in isolation, compare schema, binding and checkpoints, then reconcile broker facts newer than the snapshot. A restore to the canonical production paths requires a separate authorized cutover.
4. Use `sudo phoenix-control rollback PREVIOUS_FULL_SHA` only with the Phoenix units already stopped, their position ownership and protection accounted for, and the current database compatible with the previous release. Rollback switches code and keeps the current database. A schema disagreement requires a specific migration or recovery plan.
5. Record recovery time, recovered checkpoint, cause, owner and follow-up. Reverify reconciliation, learning, external alert delivery, and the latest scheduled backup before clearing the operational incident.
