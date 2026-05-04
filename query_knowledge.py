#!/usr/bin/env python3
"""
Quick knowledge graph query CLI.

Usage:
    python query_knowledge.py stats          # Show database stats
    python query_knowledge.py patterns       # List all patterns
    python query_knowledge.py cases <file>   # Show cases for a file
    python query_knowledge.py profile <file> # Show file risk profile
"""

import sys

from knowledge_graph import KnowledgeGraph


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    kg = KnowledgeGraph(".claude/knowledge.db")

    if cmd == "stats":
        stats = kg.stats()
        print(f"Knowledge Graph Statistics")
        print(f"  Patterns:      {stats['patterns']}")
        print(f"  Cases:         {stats['cases']}")
        print(f"  File Profiles: {stats['file_profiles']}")
        print(f"  Scan Runs:     {stats['scan_runs']}")

    elif cmd == "patterns":
        patterns = kg.get_all_patterns()
        print(f"Patterns ({len(patterns)}):")
        for p in patterns:
            rule = f"[{p.rule_id}] " if p.rule_id else ""
            print(f"  {p.id}: {rule}{p.content[:80]}")

    elif cmd == "cases" and len(sys.argv) >= 3:
        file_path = sys.argv[2]
        cases = kg.get_cases_by_file(file_path)
        print(f"Cases for {file_path} ({len(cases)}):")
        for c in cases:
            status = "✓" if c.status == "confirmed" else "✗" if c.status == "false_positive" else "?"
            print(f"  [{status}] [{c.rule_id}] {c.message[:80]}")

    elif cmd == "profile" and len(sys.argv) >= 3:
        file_path = sys.argv[2]
        profile = kg.get_file_profile(file_path)
        if profile:
            print(f"File Profile: {file_path}")
            print(f"  Total issues: {profile.total_issues}")
            print(f"  Risk score:   {profile.risk_score:.1f}/10")
            print(f"  Last scan:    {profile.last_scan_at}")
            print(f"  Top patterns: {', '.join(profile.top_patterns[:5])}")
        else:
            print(f"No profile found for {file_path}")

    else:
        print(__doc__)
        sys.exit(1)

    kg.close()


if __name__ == "__main__":
    main()
