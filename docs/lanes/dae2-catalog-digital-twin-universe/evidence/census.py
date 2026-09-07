#!/usr/bin/env python3
"""Reproducible census for the dae2 delegate-catalog lane.

Run from the repo root:  python3 /tmp/census_dae2.py
Body = everything after the closing `---` of the YAML frontmatter.
"""
import hashlib
import json
import subprocess
import sys

import yaml

AGENT = "agents/dtu-profile-builder.md"

def split(raw: str):
    assert raw.startswith("---")
    end = raw.index("\n---", 3)
    return raw[3:end], raw[end + 4:]

def census(label: str, raw: str) -> dict:
    fm, body = split(raw)
    d = yaml.safe_load(fm)
    desc = d["meta"]["description"]
    return {
        "arm": label,
        "agent": d["meta"]["name"],
        "desc_chars": len(desc),
        "desc_bytes": len(desc.encode()),
        "desc_chars_stripped": len(desc.strip()),
        "est_tokens_chars_div_4": round(len(desc) / 4),
        "example_blocks": desc.count("<example>"),
        "commentary_blocks": desc.count("<commentary>"),
        "body_chars": len(body),
        "body_md5": hashlib.md5(body.encode()).hexdigest(),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "model_role": d.get("model_role"),
        "provider_preferences": d.get("provider_preferences"),
    }

stock_raw = subprocess.run(
    ["git", "show", "origin/main:" + AGENT], capture_output=True, text=True, check=True
).stdout
lean_raw = open(AGENT, encoding="utf-8").read()

stock = census("STOCK (origin/main)", stock_raw)
lean = census("LEAN (branch)", lean_raw)
out = {
    "origin_main_sha": subprocess.run(
        ["git", "rev-parse", "origin/main"], capture_output=True, text=True, check=True
    ).stdout.strip(),
    "agents_discovered_this_repo": 1,
    "stock": stock,
    "lean": lean,
    "delta_desc_chars": lean["desc_chars"] - stock["desc_chars"],
    "delta_desc_pct": round((lean["desc_chars"] - stock["desc_chars"]) / stock["desc_chars"] * 100, 1),
    "body_byte_identical": stock["body_md5"] == lean["body_md5"],
    "non_description_frontmatter_unchanged": (
        stock["model_role"] == lean["model_role"]
        and stock["provider_preferences"] == lean["provider_preferences"]
    ),
}
json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
print()
