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

Stage 2 observations (no source modifications; logs in `/workspaces/av/.tmp/`):

For the demo target a clean build with `--config=nocache` (busts disk cache + `--noremote_accept_cached`) yields 6359 actions: 5023 bazel-internal + 1090 remote-executed + 246 local sandboxed. Only the latter 1336 actions cross the REAPI boundary. The grpc log (`--remote_grpc_log`) shows: 1 GetCapabilities, 1336 FindMissingBlobs, 1090 Execute, 1090 ByteStream.Read, 1378 ByteStream.Write, 246 UpdateActionResult.

The 5023 internal actions write outputs directly from Java and never produce an REAPI `Action` proto. On the demo target specifically they are precisely the actions that build the runtime entrypoint: the bash launcher (`TemplateExpand` → `bazel-bin/.../demo`), the runfiles index (`SourceSymlinkManifest` → `demo.runfiles_manifest`), the loader and stage2 bootstrap (`TemplateExpand`), the repo mapping (`RepoMappingManifest`), the venv configs (`FileWrite`), the `_solib_k8/` symlinks (`SolibSymlink`/`UnresolvedSymlink`), and the runfiles aggregator (`Middleman`). None of these outputs get uploaded to CAS by stock bazel.

What does land in CAS for this target: outputs of the `PyCompile` actions (`.pyc` for every Python file) and outputs of the `PatchRUNPATH` actions (RPATH-rewritten `.so` files). Together they cover the bulk content of the runfiles tree. The missing piece is the small set of bazel-generated glue files above — most importantly `demo.runfiles_manifest`, which is the index mapping every runfile path to a concrete file. Without it (and the launcher), the CAS blobs cannot be reassembled into a working binary.

There is no single REAPI Action digest today that represents an `av_py_binary` and roots a Merkle walk to every blob; the terminal `Middleman`/`SourceSymlinkManifest`/launcher `TemplateExpand` actions have no REAPI Action proto. Exposing such a digest (or upgrading those internal actions to spawnable ones) is the next concrete instrumentation target.

Detailed counts and digest examples live in `/workspaces/av/.tmp/stage2_findings.md`.

Genrule trick — a `genrule` whose `tools` attribute references the binary becomes a single REAPI Action whose `input_root_digest` is a Merkle root over the binary's full runfiles tree. Stock bazel emits the action digest in the spawn log; no source changes required. For the `av` repo we added:

    genrule(
        name = "demo_via_genrule",
        outs = ["demo_output.txt"],
        cmd = "$(location :demo) > $@",
        tools = [":demo"],
    )

Reproduction (uses self-built bazel + `reapi/split_digests.py` in this repo):

    /workspaces/bazel/bazel-bin/src/bazel --output_base=/home/vscode/.cache/bazel-from-source clean
    /workspaces/bazel/bazel-bin/src/bazel \
      --output_base=/home/vscode/.cache/bazel-from-source \
      build //junk/kuddai/demo:demo_via_genrule \
      --config=remote --config=nocache --remote_upload_local_results=true \
      --execution_log_json_file=/workspaces/av/.tmp/exec_genrule.json \
      --remote_grpc_log=/workspaces/av/.tmp/grpc_genrule.binlog
    python3 /workspaces/bazel/reapi/split_digests.py \
      /workspaces/av/.tmp/exec_genrule.json \
      /workspaces/av/.tmp/grpc_genrule.binlog \
      /workspaces/av/.tmp

`--config=nocache` (defined in `/workspaces/av/.bazelrc:91`) sets `--disk_cache= --noremote_accept_cached` so every action runs fresh and the REAPI traffic is observable. `--remote_upload_local_results=true` makes locally-sandboxed actions upload their outputs to CAS. The grpc log is a stream of length-delimited `remote_logging.LogEntry` protos (proto at `src/main/protobuf/remote_execution_log.proto`); `split_digests.py` deliberately avoids a real proto parser by grep-matching 64-char hex hashes anywhere in the binary log, which is a conservative superset of "blob known to CAS."

Planned next step — Option 3: extend `genrule` so the REAPI Action digest is a first-class declared output of the rule. After patching, a consumer can write `data = [":demo_via_genrule.reapi_action_digest"]` (or similar) and the file shows up in runfiles like any other output. Required touchpoints:

- `src/main/java/com/google/devtools/build/lib/rules/genrule/GenRuleBaseRule.java` — add the rule attribute (e.g. `expose_reapi_action_digest`) to the genrule schema.
- `src/main/java/com/google/devtools/build/lib/rules/genrule/GenRuleBase.java:71` — `filesToBuild` is constructed from `ruleContext.getOutputArtifacts()`; when the new attribute is set, declare an extra implicit output Artifact (e.g. `<name>.reapi_action_digest`) and add it to `filesToBuild`.
- `src/main/java/com/google/devtools/build/lib/rules/genrule/GenRuleAction.java:34` — `GenRuleAction extends SpawnAction`; either subclass it or add a writeable extra-output that the remote/local execution path populates with the digest content.
- `src/main/java/com/google/devtools/build/lib/remote/RemoteExecutionService.java:593` — emit point. Right after `ActionKey actionKey = digestUtil.computeActionKey(action);`, write `<hash> <size>\n` to the extra-output Artifact path.

Per-target sidecar means no whole-build aggregate file, no Starlark wrapper rule, and the file participates in normal bazel caching/invalidation. Cost: touches three places in the rule definition plus one line in the REAPI layer.

Observed for the genrule build (4335 actions: 3097 internal + 1091 remote + 147 sandboxed):

  - Genrule Action digest: `f3a9c60a1a3005c995ee6e917ab82417c8ecc099c60ff9049f36056386cac898` (size 148).
  - 9684 (path, file-digest) pairs across logged spawns; 9525 of those file digests appear in the grpc log → present in CAS. The 159 not-in-CAS entries are inputs of locally-sandboxed PatchRUNPATH actions (Nix-store source `.so`/interpreter files); nothing remote references them directly, so bazel never uploads them.
  - 1238 unique REAPI Action digests in the spawn log; all 1238 appear in the grpc log.
  - The bazel-internal action outputs (launcher, `demo.runfiles_manifest`, repo_mapping, venv configs, `_solib_k8/` symlinks) are not produced by any REAPI Action, but they DO end up in CAS — bazel uploads them as input blobs when constructing the genrule's `input_root_digest`. So most of what's needed to reconstruct the binary tree is already addressable from the single Genrule Action digest above; walking its Merkle DAG recursively yields every blob the worker needs to execute it.
