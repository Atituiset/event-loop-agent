#!/usr/bin/env python3
"""
Initialize the knowledge graph database from existing knowledge sources.

Usage:
    python init_knowledge.py

This imports:
  - skills/wireless-radio.md  → Pattern nodes
"""

from pathlib import Path

from knowledge_graph import KnowledgeGraph


def main():
    db_path = ".claude/knowledge.db"
    md_path = "knowleage/wireless-radio.md"

    print(f"Initializing knowledge graph: {db_path}")
    kg = KnowledgeGraph(db_path)

    # Import from wireless-radio.md
    if Path(md_path).exists():
        count = kg.import_from_wireless_radio_md(md_path)
        print(f"Imported {count} patterns from {md_path}")
    else:
        print(f"Warning: {md_path} not found")

    # Show stats
    stats = kg.stats()
    print(f"Knowledge graph stats: {stats}")

    # List all patterns
    patterns = kg.get_all_patterns()
    print(f"\nPatterns ({len(patterns)}):")
    for p in patterns:
        print(f"  {p.id}: {p.content[:60]}... (confidence={p.confidence})")

    kg.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
