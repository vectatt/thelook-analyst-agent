"""Prompt layers, owned by different people, composed per tool, re-read every turn.

The brief requires that a non-developer can change the agent's instructions without a redeploy. That
only works if the words live in files rather than in Python — and if the files are split by *who owns
them*, because the person who rewrites the report voice each week is not the person who decides what
counts as revenue, and neither of them should be editing SQL rules.

    persona.md       tone and voice                         CEO / marketing   (the weekly change)
    report.md        report structure and policy            analyst + CEO
    conventions.md   business rules for this dataset        analyst
    sql.md           how to write SQL here                  engineer
    agent.md         which tools, when                      engineer

Every file is read on each use, so an edit takes effect on the next message. Each is content-hashed
and the hashes are attached to the trace, so "reports got worse this week" is an answerable question.

`persona.md` may also carry front-matter policy, which the report post-hook *enforces* rather than
merely suggests:

    ---
    max_words: 300
    require_sections: [headline, findings, action_items]
    ---
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from analyst.config import settings

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class Prompt:
    name: str
    text: str
    policy: dict[str, object] = field(default_factory=dict)
    version: str = ""


def _parse_policy(raw: str) -> tuple[str, dict[str, object]]:
    """Split optional YAML-ish front matter from the body. Deliberately tiny: keys, scalars and lists."""
    m = _FRONT_MATTER.match(raw)
    if not m:
        return raw, {}
    policy: dict[str, object] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            policy[key.strip()] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        elif value.isdigit():
            policy[key.strip()] = int(value)
        elif value.lower() in ("true", "false"):
            policy[key.strip()] = value.lower() == "true"
        else:
            policy[key.strip()] = value.strip("'\"")
    return raw[m.end():], policy


def load(name: str, fallback: str = "", directory: Path | None = None) -> Prompt:
    """Read one prompt layer. Missing files fall back rather than crashing a live session."""
    path = (directory or settings.prompts_dir) / f"{name}.md"
    try:
        raw = path.read_text()
    except OSError:
        return Prompt(name=name, text=fallback, version="missing")
    body, policy = _parse_policy(raw)
    return Prompt(name=name, text=body.strip(), policy=policy,
                  version=hashlib.sha1(raw.encode()).hexdigest()[:8])


def compose(*names: str, directory: Path | None = None) -> tuple[str, dict[str, object], dict[str, str]]:
    """Compose several layers into one system prompt.

    Returns the text, the merged policy (later layers win) and {layer: version} for tracing.
    """
    texts, policy, versions = [], {}, {}
    for name in names:
        p = load(name, directory=directory)
        if p.text:
            texts.append(p.text)
        policy.update(p.policy)
        versions[name] = p.version
    return "\n\n".join(texts), policy, versions
