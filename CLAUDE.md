<!-- Notes for Claude. Save only true facts verified against the source tree at the time of writing. Avoid excessive markdown, headers, or speculation. Prune stale or irrelevant facts when they no longer match the code. -->

Goal of this branch: produce a bazel binary that emits an explanation of every digest it submits to the remote CAS via REAPI, so the user can download those digests directly from CAS and reassemble the build output binary locally without re-running bazel.

Branch `expose-reapi-digest` (fork `git@github.com:kuddai/bazel.git`) tracks experiments to expose Remote Execution API digests.

Bazel flag `--remote_upload_local_results` is defined in `src/main/java/com/google/devtools/build/lib/remote/options/RemoteOptions.java:269`.

When true, results of locally-executed actions are uploaded to the remote cache. The gate is `Utils.shouldUploadLocalResultsToRemoteCache` at `src/main/java/com/google/devtools/build/lib/remote/util/Utils.java:576-581` — requires `remoteUploadLocalResults && mayBeCachedRemotely() && !NO_REMOTE_CACHE_UPLOAD`.

Write-policy decision lives in `RemoteExecutionService.getWriteCachePolicy` at `src/main/java/com/google/devtools/build/lib/remote/RemoteExecutionService.java:317-329`.

Trigger point for locally-executed spawns is `RemoteSpawnCache.store` at `src/main/java/com/google/devtools/build/lib/remote/RemoteSpawnCache.java:233-290`.

Actual upload happens in `UploadManifest.uploadAsync` at `src/main/java/com/google/devtools/build/lib/remote/UploadManifest.java:677-728`. It writes to two stores against the configured `--remote_cache`/`--remote_executor` gRPC endpoint (e.g. Buildbarn):
- CAS: output file blobs, output directories as `Tree` protos, stdout/stderr blobs, action and command protos. See `addFile` ~249, `addDirectory` ~564, `setStdoutStderr` ~200, action/command blobs ~306.
- Action Cache: one `ActionResult` per successful action, keyed by action digest, only when `exit_code == 0` (~712-725).

Remote-executed actions are not uploaded via this flag — the remote executor populates the AC itself.

`--remote_local_fallback` results still respect `--remote_upload_local_results` (gate is shared).
