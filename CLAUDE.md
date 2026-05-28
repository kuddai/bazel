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

REAPI Merkle model (verified in `third_party/remoteapis/build/bazel/remote/execution/v2/remote_execution.proto`):
- `Action` (line 480) holds `command_digest` and `input_root_digest`. The latter points to a `Directory` (854), whose `repeated DirectoryNode directories` reference child `Directory` blobs by digest, forming a Merkle DAG of inputs.
- `ActionResult` (1056), keyed by Action digest in the AC, lists `output_files` (each with digest), `output_directories` (each a `Tree` digest), `stdout_digest`, `stderr_digest`. `Tree` (1259) is one blob containing root `Directory` plus all descendants flattened.
- Consequence: one Action digest is a sufficient root. Walking Action → Command + input Merkle tree + ActionResult outputs reaches every blob the action needed and produced. The protocol already supports the "one digest, recursively download everything" workflow; bazel just doesn't currently surface the digest for a given target.

For an `av_py_binary` like `//junk/kuddai/demo:main`, bazel does not emit a single "build the whole binary" action — the target is split into many per-rule actions, each with its own digest. The candidate single root is the terminal py_binary action (manifest/launcher generation), whose declared inputs include the full runfiles set, so its `input_root_digest` Merkle-covers every runfile (`.so`, interpreter, `.py`). Surfacing that one digest is the concrete first deliverable of this branch.

Stage 1 (current): build bazel from this source tree as-is (no source modifications) and use the resulting binary to build and run `//junk/kuddai/demo:main` in `/workspaces/av`. That gives a clean, unmodified baseline before any instrumentation.

Test harness: the self-built binary at `/workspaces/bazel/bazel-bin/src/bazel` is invoked against the demo target with a dedicated server to avoid clashing with the workspace's bazelisk-spawned one — e.g.:

    /workspaces/bazel/bazel-bin/src/bazel \
      --output_base=/home/vscode/.cache/bazel-from-source \
      build //junk/kuddai/demo \
      --config=remote --remote_upload_local_results=true

`--config=remote` resolves (per `/workspaces/av/.bazelrc:93-102`) to Buildbarn at `grpc://frontend.kansas-gimel.avride.ai:19080` for both `--remote_cache` and `--remote_executor`. Default in that repo is `--remote_upload_local_results=false`; we override to `true` so locally-executed actions actually upload to the shared CAS/AC. The target name in the BUILD file is `demo` (not `main`); the source file is `main.py`.
