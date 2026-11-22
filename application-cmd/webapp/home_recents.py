"""Group uploaded WSIs into Codex-style home-sidebar projects."""
from __future__ import annotations

import re

ACCESSION_RE = re.compile(r'^(\d{4,})[-_]')
TCGA_CASE_RE = re.compile(r'^(TCGA-[A-Z0-9]+-[A-Z0-9]+)', re.I)


def project_affinity_key(filename: str) -> str:
    """Return a patient/case key from the filename, never an organ site."""
    name = str(filename or '').strip()
    if not name:
        return ''
    accession = ACCESSION_RE.match(name)
    if accession:
        return f'accession:{accession.group(1)}'
    tcga = TCGA_CASE_RE.match(name)
    if tcga:
        return f'tcga:{tcga.group(1).upper()}'
    return ''


def _sort_files(files: list[dict]) -> list[dict]:
    return sorted(
        files,
        key=lambda item: str(item.get('upload_time') or ''),
        reverse=True,
    )


def _make_project(members: list[dict], kind: str, key: str) -> dict:
    files = _sort_files(members)
    lead = files[0] if files else {}
    return {
        'id': key,
        'kind': kind,
        'title': lead.get('filename') or '',
        'slide_count': len(files),
        'updated_at': lead.get('upload_time') or '',
        'thumbnail_url': lead.get('thumbnail_url') or '',
        'files': files,
        'lead_filename': lead.get('filename') or '',
    }


def group_home_case_projects(files: list[dict] | None) -> list[dict]:
    """Collapse same-patient / same-batch slides into one Patient Level project."""
    items = [item for item in (files or []) if item and item.get('file_id')]
    used: set[str] = set()
    projects: list[dict] = []

    by_batch: dict[str, list[dict]] = {}
    for item in items:
        batch_id = str(item.get('batch_id') or '').strip()
        if not batch_id:
            continue
        by_batch.setdefault(batch_id, []).append(item)
    for batch_id, members in by_batch.items():
        if len(members) < 2:
            continue
        projects.append(_make_project(members, 'patient', f'batch:{batch_id}'))
        used.update(str(member['file_id']) for member in members)

    remaining = [item for item in items if str(item['file_id']) not in used]
    by_affinity: dict[str, list[dict]] = {}
    singles: list[dict] = []
    for item in remaining:
        key = project_affinity_key(str(item.get('filename') or ''))
        if key:
            by_affinity.setdefault(key, []).append(item)
        else:
            singles.append(item)
    for key, members in by_affinity.items():
        if len(members) >= 2:
            projects.append(_make_project(members, 'patient', key))
        else:
            singles.extend(members)

    for item in singles:
        projects.append(_make_project([item], 'slide', f"slide:{item['file_id']}"))

    projects.sort(key=lambda item: str(item.get('updated_at') or ''), reverse=True)
    return projects
