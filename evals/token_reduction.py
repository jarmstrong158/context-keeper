"""Token-reduction measurement: session-start injection vs. full-store dump.

Answers "what does context-keeper cost my context window?" with a number:
for each store, compare the tokens actually injected at session start
(the get_project_summary output the SessionStart hook prints) against the
naive baseline of dumping every active entry into context.

Methodology caveats, stated up front:
- Token counts use the server's own estimate (len(text) // 4), not a real
  tokenizer. Both sides of the comparison use the same estimate, so the
  ratio is meaningful even if absolute counts are approximate.
- The summary is capped by a token budget (default 2000), so for large
  stores part of the reduction is by construction. The interesting number
  is the absolute injected cost staying flat while stores grow.

Usage:
    python token_reduction.py <project_dir> [<project_dir> ...]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


def measure(project_dir):
    base = os.path.join(project_dir, ".context")
    if not os.path.isdir(base):
        return None
    full_text_parts = []
    total_entries = 0
    for name in ("decisions", "pipelines", "constraints"):
        entries = server.read_json_file(os.path.join(base, name + ".json"))
        active = [e for e in entries if e.get("status", "active") != "deprecated"]
        total_entries += len(active)
        full_text_parts.append(json.dumps(active, indent=2))
    full_tokens = server.estimate_tokens("\n".join(full_text_parts))

    summary = server.handle_get_project_summary({"project_dir": project_dir})
    injected_text = (summary.get("summary") or "") + "\n" + (summary.get("usage_guidance") or "")
    injected_tokens = server.estimate_tokens(injected_text)

    reduction = 100.0 * (1 - injected_tokens / full_tokens) if full_tokens else 0.0
    return {
        "store": os.path.basename(os.path.normpath(project_dir)),
        "active_entries": total_entries,
        "full_store_tokens": full_tokens,
        "injected_tokens": injected_tokens,
        "reduction_pct": round(reduction, 1),
    }


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)
    rows = []
    for p in paths:
        r = measure(p)
        if r is None:
            print(f"skip {p}: no .context/ store")
            continue
        rows.append(r)

    print(f"\n| store | active entries | full store (tokens) | injected at session start | reduction |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['store']} | {r['active_entries']} | ~{r['full_store_tokens']:,} "
              f"| ~{r['injected_tokens']:,} | {r['reduction_pct']}% |")
    print("\nBaseline = dumping every active entry (pretty-printed JSON) into context.")
    print("Injected = get_project_summary output, what the SessionStart hook prints.")


if __name__ == "__main__":
    main()
