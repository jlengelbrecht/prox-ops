# Backlog reader temp_file_limit cap

`role-limits.sql` sets one role default, `temp_file_limit = 128MB`, on
`hindsight_backlog_metrics`. It is a per-backend safety cap on temporary-file
disk use. It does not bound RAM and it is not a sizing or worker-scaling
target. `temp_file_limit` is superuser-context, so the reader cannot raise,
reset or override it, and no SET grant on the parameter exists.

The Job `hindsight-backlog-role-limits-<sha12>-r<N>` runs the file once as the
cluster's existing superuser Secret over verify-full TLS. Flux applies it only
after `hindsight-backlog-db-acl`, whose TEMPORARY revoke the guard requires.
Nothing depends on this tree.

## What the guard proves, and what it does not

- Reader grants: none, or exactly USAGE on the backlog function's schema and
  EXECUTE on its zero-argument function, both owner objects granted by the
  owner without grant option. CREATE on any schema, granted directly or to
  PUBLIC, is refused.
- pg_hba: the two reader rules exist, in order, in a file with no parse
  errors. This is configuration evidence only. An earlier rule matching the
  reader would still decide its logins; the guard cannot see that, and the
  native proof shows it passing in that case. Check the live rule order
  separately.
- Sessions: the guard refuses while a reader session is connected, but a
  login between that check and the commit is not seen.

## After the Job completes

Before anything connects as the reader, in a fresh read-only transaction:

1. Zero `hindsight_backlog_metrics` rows in `pg_stat_activity`. A session
   that existed before the commit keeps its old setting.
2. A new reader session shows `temp_file_limit` `128MB` with source `user`
   and setting `131072`, `has_database_privilege(..., 'TEMPORARY')` false,
   and `has_parameter_privilege(..., 'temp_file_limit', 'SET')` false.

The setting lives in the role catalog on the retained database volume.
Whether a CNPG role or Database reconcile preserves it is unknown until
observed after one happens naturally; do not trigger one to find out.

## Rollback

Reverting this tree or pruning the Job does not undo the setting. Roll back
with a compensating change:

1. Withdraw every consumer of the reader and its function grants (none
   today).
2. Verify zero reader sessions and the exact authorized state: TEMPORARY
   false, the accepted grants only, no parameter privilege.
3. Ship a new, separately reviewed SQL file with one fixed
   `ALTER ROLE hindsight_backlog_metrics RESET temp_file_limit`, under the
   same guards, with pre- and post-image checks that keep the five approved
   session defaults, every other role setting and the parameter ACL
   unchanged, and its own hash-named Job revision.

Do not ship a standing RESET Job, and do not weaken the guards in this file to
make a rollback pass.
