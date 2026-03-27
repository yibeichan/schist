"""Markdown I/O — read, write, and manipulate vault notes."""

import re

import frontmatter


def slugify(title: str) -> str:
    """Lowercase, spaces to hyphens, strip non-alphanum except hyphens."""
    s = title.lower().strip()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.strip('-')


def read_note(path: str) -> dict:
    """Read a markdown note, return dict with 'frontmatter' and 'body' keys."""
    post = frontmatter.load(path)
    return {'frontmatter': dict(post.metadata), 'body': post.content}


def write_note(path: str, fm: dict, body: str):
    """Write a markdown note with YAML frontmatter."""
    post = frontmatter.Post(body, **fm)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.dumps(post) + '\n')


def append_connection(path: str, connection_type: str, target: str, context: str | None = None):
    """Append a connection line to the ## Connections section (creating it if needed)."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    if context:
        line = f'- {connection_type}: {target} "{context}"'
    else:
        line = f'- {connection_type}: {target}'

    if '## Connections' in content:
        # Find the section and append before the next ## or end of file
        lines = content.split('\n')
        insert_idx = None
        in_section = False
        for i, ln in enumerate(lines):
            if ln.strip().startswith('## Connections'):
                in_section = True
                continue
            if in_section and ln.strip().startswith('## '):
                insert_idx = i
                break
        if insert_idx is None:
            # Append at end
            lines.append(line)
        else:
            lines.insert(insert_idx, line)
        content = '\n'.join(lines)
    else:
        # Create section at end
        content = content.rstrip() + '\n\n## Connections\n' + line + '\n'

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
