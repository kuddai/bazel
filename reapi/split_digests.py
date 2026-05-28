"""Parse bazel --execution_log_json_file + --remote_grpc_log into per-row
digest inventories.

Usage:
    python3 split_digests.py <exec.json> <grpc.binlog> <out_dir>

Writes three files into <out_dir>:
    digests_in_cas.txt      hash size path     # digest hash seen in grpc log
    digests_local_only.txt  hash size path     # digest hash never on the wire
    action_digests.txt      hash size mnemonic targetLabel (CAS|MISSING)
"""

import json
import re
import sys
from pathlib import Path

HEX = re.compile(rb"[0-9a-f]{64}")


def main(exec_path: str, grpc_path: str, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cas_hashes = {
        m.group(0).decode() for m in HEX.finditer(Path(grpc_path).read_bytes())
    }

    text = Path(exec_path).read_text()
    records = []
    depth = 0
    start = None
    for i, c in enumerate(text):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                records.append(text[start : i + 1])

    path_digests: dict[tuple[str, str], str] = {}
    action_digests: list[tuple[str, str, str, str]] = []
    for raw in records:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ad = rec.get("digest")
        if ad:
            action_digests.append(
                (
                    ad["hash"],
                    str(ad.get("sizeBytes", "?")),
                    rec.get("mnemonic", "?"),
                    rec.get("targetLabel", "?"),
                )
            )
        for kind in ("inputs", "actualOutputs"):
            for entry in rec.get(kind, []):
                d = entry.get("digest")
                if d and "hash" in d:
                    path_digests.setdefault(
                        (entry.get("path", ""), d["hash"]),
                        str(d.get("sizeBytes", "?")),
                    )

    in_cas_path = out / "digests_in_cas.txt"
    local_path = out / "digests_local_only.txt"
    action_path = out / "action_digests.txt"

    with in_cas_path.open("w") as fa, local_path.open("w") as fb:
        for (path, h), size in sorted(path_digests.items()):
            line = f"{h} {size} {path}\n"
            (fa if h in cas_hashes else fb).write(line)

    with action_path.open("w") as f:
        f.write("# action_digest size mnemonic targetLabel (in_cas?)\n")
        for h, sz, mn, tl in action_digests:
            flag = "CAS" if h in cas_hashes else "MISSING"
            f.write(f"{h} {sz} {mn} {tl} {flag}\n")

    in_cas_cnt = sum(1 for (_, h) in path_digests if h in cas_hashes)
    miss_action = sum(1 for (h, *_t) in action_digests if h not in cas_hashes)
    print(f"cas hashes from grpc log: {len(cas_hashes)}")
    print(f"file path-digest pairs:    {len(path_digests)}")
    print(f"  in cas:                  {in_cas_cnt}")
    print(f"  local only:              {len(path_digests) - in_cas_cnt}")
    print(f"unique action digests:     {len(action_digests)}")
    print(f"  not in cas:              {miss_action}")
    print(f"wrote: {in_cas_path}, {local_path}, {action_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
