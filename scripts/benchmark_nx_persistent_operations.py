from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import statistics
import sys
import time
import uuid
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from webapp.backend.app.nx_persistent_worker import PersistentNXWorker
from webapp.backend.app.nx_runtime_contracts import PublicRuntimeExecuteRequest, RuntimeMode


DISCOVER_CODE = r'''
import NXOpen.Features

def face_row(face):
    try:
        raw_type = str(face.SolidFaceType).rsplit('.', 1)[-1]
        face_type = {
            '1': 'Planar', '2': 'Cylindrical', '3': 'Conical',
            '4': 'Spherical', '5': 'Torus', '6': 'BSurface',
        }.get(raw_type, raw_type)
    except Exception:
        face_type = 'unknown'
    return {
        'tag': int(face.Tag),
        'face_type': face_type,
        'edge_count': len(face.GetEdges()),
    }

faces = []
face_objects = {}
for body in workPart.Bodies:
    for face in body.GetFaces():
        row = face_row(face)
        faces.append(row)
        face_objects[row['tag']] = face
curved_tags = set(row['tag'] for row in faces if row['face_type'] in ('Cylindrical', 'Spherical', 'Torus', 'BSurface', 'Blending'))
adjacency = dict((tag, set()) for tag in curved_tags)
for tag in curved_tags:
    for edge in face_objects[tag].GetEdges():
        try:
            neighbors = list(edge.GetFaces())
        except Exception:
            neighbors = []
        for neighbor in neighbors:
            neighbor_tag = int(neighbor.Tag)
            if neighbor_tag in curved_tags and neighbor_tag != tag:
                adjacency[tag].add(neighbor_tag)
components = []
remaining = set(curved_tags)
while remaining:
    seed = min(remaining)
    remaining.remove(seed)
    stack = [seed]
    group = []
    while stack:
        tag = stack.pop()
        group.append(tag)
        for neighbor in sorted(adjacency[tag] & remaining):
            remaining.remove(neighbor)
            stack.append(neighbor)
    components.append(sorted(group))
components.sort(key=lambda group: (group[0], len(group)))
result = {
    'body_count': len(list(workPart.Bodies)),
    'face_count': len(faces),
    'faces': faces,
    'curved_components': components,
}
'''


def deletion_code(tags: list[int], select_type: str, preference: str) -> str:
    return r'''
import NXOpen.Features
wanted = set(%s)
faces = []
for body in workPart.Bodies:
    for face in body.GetFaces():
        if int(face.Tag) in wanted:
            faces.append(face)
if len(faces) != len(wanted):
    raise RuntimeError('selected face tags are no longer current')
builder = workPart.Features.CreateDeleteFaceBuilder(NXOpen.Features.Feature.Null)
try:
    builder.FaceRecognized.RelationScope = 0
    builder.FaceRecognized.CloneScope = 1535
    builder.FaceRecognized.CoplanarEnabled = False
    builder.FaceRecognized.CoplanarAxesEnabled = False
    builder.FaceRecognized.CoaxialEnabled = False
    builder.FaceRecognized.SameOrbitEnabled = False
    builder.FaceRecognized.EqualDiameterEnabled = False
    builder.FaceRecognized.TangentEnabled = False
    builder.FaceRecognized.SymmetricEnabled = False
    builder.FaceRecognized.OffsetEnabled = False
    builder.FaceRecognized.RigidBodyFaceEnabled = False
    builder.FaceRecognized.UseFindClone = True
    builder.FaceRecognized.UseFindRelated = False
    builder.FaceRecognized.UseFaceBrowse = True
    builder.MaxHoleDiameter.SetFormula('5')
    builder.MaxBlendRadius.SetFormula('5')
    builder.DeletePartialBlend = False
    builder.Type = NXOpen.Features.DeleteFaceBuilder.SelectTypes.%s
    builder.FaceEdgeBlendPreference = NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.%s
    builder.CapPlane = NXOpen.Plane.Null
    options = workPart.ScRuleFactory.CreateRuleOptions()
    try:
        options.SetSelectedFromInactive(False)
        rule = workPart.ScRuleFactory.CreateRuleFaceDumb(faces, options)
    finally:
        options.Dispose()
    builder.FaceCollector.ReplaceRules([rule], False)
    builder.FaceRecognized.SelectEntities(faces)
    committed = builder.Commit()
    if committed is None:
        raise RuntimeError('DeleteFaceBuilder.Commit returned None')
    result = {'selected_tags': sorted(wanted), 'selected_count': len(faces)}
finally:
    builder.Destroy()
''' % (repr(tags), select_type, preference)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Benchmark read/delete-face/delete-fillet operations in one persistent NX process.'
    )
    parser.add_argument('--input', type=Path, required=True, help='Input STEP file')
    parser.add_argument('--output-dir', type=Path, default=REPO_ROOT / 'benchmark-results')
    parser.add_argument(
        '--repetitions', type=int, default=10,
        help='Number of distinct sampled face groups per operation type.',
    )
    parser.add_argument('--seed', type=int, default=2406, help='Deterministic sampling seed')
    parser.add_argument('--timeout', type=float, default=180.0)
    parser.add_argument(
        '--three-plus-count', type=int, default=3,
        help='Number of ordinary faces or distinct curved components used by explicit 3-plus cases (minimum 3).',
    )
    return parser.parse_args()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 6)


def summarize(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in cases:
        groups.setdefault(row['case'], []).append(row)
    result = []
    for name, rows in groups.items():
        deletion = [float(row['deletion_seconds']) for row in rows]
        end_to_end = [float(row['end_to_end_seconds']) for row in rows]
        states = Counter(row['state'] for row in rows)
        result.append({
            'case': name,
            'attempts': len(rows),
            'successes': sum(row['success'] for row in rows),
            'failures': sum(not row['success'] for row in rows),
            'states': dict(sorted(states.items())),
            'mean_deletion_seconds': round(statistics.fmean(deletion), 6),
            'median_deletion_seconds': round(statistics.median(deletion), 6),
            'min_deletion_seconds': round(min(deletion), 6),
            'max_deletion_seconds': round(max(deletion), 6),
            'p95_deletion_seconds': percentile(deletion, 0.95),
            'mean_end_to_end_seconds': round(statistics.fmean(end_to_end), 6),
        })
    return result


def _sample_unique_groups(
    population: list[Any], group_size: int, sample_count: int, rng: random.Random
) -> list[list[Any]]:
    if len(population) < group_size:
        return []
    groups: list[list[Any]] = []
    seen: set[tuple[Any, ...]] = set()
    max_attempts = max(100, sample_count * 100)
    for _ in range(max_attempts):
        group = rng.sample(population, group_size)
        key = tuple(sorted(group))
        if key in seen:
            continue
        seen.add(key)
        groups.append(group)
        if len(groups) == sample_count:
            return groups
    return groups


def build_plans(
    faces: list[dict[str, Any]], components: list[list[int]], three_plus_count: int,
    sample_count: int = 10, seed: int = 2406,
) -> list[tuple[str, list[int], str, str, int]]:
    if three_plus_count < 3:
        raise ValueError('three_plus_count must be at least 3')
    if sample_count < 1:
        raise ValueError('sample_count must be positive')
    rng = random.Random(seed)
    curved_tags = [
        row['tag'] for row in faces
        if row['face_type'] in {'Cylindrical', 'Spherical', 'Torus', 'BSurface', 'Blending'}
    ]
    planar_tags = [row['tag'] for row in faces if row['face_type'] == 'Planar']
    eligible_components = [sorted(component) for component in components if component]
    plans: list[tuple[str, list[int], str, str, int]] = []

    def add_face_samples(name: str, population: list[int], size: int, select_type: str) -> None:
        groups = _sample_unique_groups(population, size, sample_count, rng)
        if len(groups) != sample_count:
            raise ValueError('{} needs {} distinct groups, found {}'.format(name, sample_count, len(groups)))
        for sample_index, tags in enumerate(groups, start=1):
            plans.append((name, sorted(tags), select_type, 'Cliff', sample_index))

    add_face_samples('fillet_single_face', curved_tags, 1, 'FaceEdgeBlend')
    add_face_samples('fillet_two_faces', curved_tags, 2, 'FaceEdgeBlend')
    add_face_samples('fillet_three_plus_faces', curved_tags, three_plus_count, 'FaceEdgeBlend')
    component_samples = _sample_unique_groups(list(range(len(eligible_components))), 1, sample_count, rng)
    if len(component_samples) != sample_count:
        raise ValueError('fillet_single_component needs {} distinct components'.format(sample_count))
    for sample_index, indices in enumerate(component_samples, start=1):
        plans.append(('fillet_single_component', eligible_components[indices[0]], 'FaceEdgeBlend', 'Cliff', sample_index))
    component_groups = _sample_unique_groups(
        list(range(len(eligible_components))), three_plus_count, sample_count, rng
    )
    if len(component_groups) != sample_count:
        raise ValueError('fillet_three_plus_components needs {} distinct groups'.format(sample_count))
    for sample_index, indices in enumerate(component_groups, start=1):
        tags = sorted(tag for index in indices for tag in eligible_components[index])
        plans.append(('fillet_three_plus_components', tags, 'FaceEdgeBlend', 'Cliff', sample_index))
    add_face_samples('generic_single_face', planar_tags, 1, 'Face')
    add_face_samples('generic_two_faces', planar_tags, 2, 'Face')
    add_face_samples('generic_three_plus_faces', planar_tags, three_plus_count, 'Face')
    add_face_samples('expected_failure_fillet_on_planar', planar_tags, 1, 'FaceEdgeBlend')
    for sample_index in range(1, sample_count + 1):
        plans.append(('expected_failure_missing_tag', [2147483647 - sample_index], 'Face', 'Cliff', sample_index))
    return plans


def operation(worker: PersistentNXWorker, code: str, *, timeout: float) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    result = worker.submit(PublicRuntimeExecuteRequest(
        operation_id='op-bench-' + uuid.uuid4().hex,
        base_state=0,
        mode=RuntimeMode.PREVIEW,
        code=code,
    ), timeout_seconds=timeout)
    return result, time.perf_counter() - started


def record_case(
    worker: PersistentNXWorker,
    rows: list[dict[str, Any]],
    *,
    name: str,
    code: str,
    selected_tags: list[int],
    sample_index: int,
    timeout: float,
) -> None:
    result, end_to_end = operation(worker, code, timeout=timeout)
    state = str(result.get('state'))
    error = result.get('error') or {}
    worker_timings = result.get('timings') or {}
    deletion_seconds = worker_timings.get('execute_seconds')
    if deletion_seconds is None:
        raise RuntimeError('persistent Worker did not return execute_seconds for {}'.format(name))
    rows.append({
        'case': name,
        'sample_index': sample_index,
        'selected_face_count': len(selected_tags),
        'selected_tags': selected_tags,
        'state': state,
        'success': state == 'rolled_back' and error.get('code') is None,
        'deletion_seconds': round(float(deletion_seconds), 6),
        'end_to_end_seconds': round(end_to_end, 6),
        'nx_update_error_count': (result.get('validation') or {}).get('nx_update_error_count'),
        'worker_timings': worker_timings,
        'error_code': error.get('code'),
        'error_message': error.get('message'),
        'result': result.get('result'),
    })


def main() -> int:
    args = parse_args()
    if args.repetitions < 1:
        raise SystemExit('--repetitions must be positive')
    if args.three_plus_count < 3:
        raise SystemExit('--three-plus-count must be at least 3')
    input_step = args.input.resolve()
    if not input_step.is_file():
        raise SystemExit('input STEP does not exist: {}'.format(input_step))
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    output_dir = args.output_dir.resolve() / run_id
    workspace = output_dir / 'worker'
    output_dir.mkdir(parents=True)
    worker_input = output_dir / 'input.step'
    worker_input.write_bytes(input_step.read_bytes())
    worker = PersistentNXWorker(workspace)
    started = time.perf_counter()
    cases: list[dict[str, Any]] = []
    try:
        heartbeat = worker.start(worker_input, startup_timeout=args.timeout)
        startup_seconds = time.perf_counter() - started
        discovery, discovery_seconds = operation(worker, DISCOVER_CODE, timeout=args.timeout)
        faces = (discovery.get('result') or {}).get('faces') or []
        curved = [row for row in faces if row['face_type'] in {'Cylindrical', 'Spherical', 'Torus', 'BSurface', 'Blending'}]
        planar = [row for row in faces if row['face_type'] == 'Planar']
        components = (discovery.get('result') or {}).get('curved_components') or []
        plans = build_plans(
            faces, components, args.three_plus_count,
            sample_count=args.repetitions, seed=args.seed,
        )
        required_cases = {
            'generic_three_plus_faces',
            'fillet_three_plus_faces',
            'fillet_three_plus_components',
        }
        missing_cases = sorted(required_cases - {plan[0] for plan in plans})
        if missing_cases:
            raise RuntimeError(
                'model does not provide enough sampled faces for required cases: {}'.format(', '.join(missing_cases))
            )

        for name, tags, select_type, preference, sample_index in plans:
            code = deletion_code(tags, select_type, preference)
            record_case(
                worker, cases, name=name, code=code, selected_tags=tags,
                sample_index=sample_index, timeout=args.timeout,
            )

        report = {
            'schema': 'nx_persistent_operation_benchmark_v2',
            'run_id': run_id,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'input': str(input_step),
            'worker_pid': heartbeat.get('pid'),
            'worker_build': heartbeat.get('worker_build'),
            'startup_seconds': round(startup_seconds, 6),
            'discovery_seconds': round(discovery_seconds, 6),
            'baseline': discovery.get('baseline'),
            'face_inventory': {
                'total': len(faces),
                'curved': len(curved),
                'planar': len(planar),
                'types': dict(sorted(Counter(row['face_type'] for row in faces).items())),
            },
            'method': {
                'mode': 'preview',
                'isolation': 'every deletion is undone and restoration-verified',
                'primary_timing': 'deletion_seconds is Worker execute_seconds around face lookup, DeleteFaceBuilder setup/Commit, and builder destruction, whether deletion succeeds or fails',
                'excluded_from_primary_timing': 'queue polling, model signatures, Undo, restoration validation, and result transport',
                'diagnostic_timing': 'end_to_end_seconds and worker_timings are retained for overhead analysis only',
                'warning': 'face sets are deterministic samples, not guaranteed semantic fillet components',
                'distinct_samples_per_operation': args.repetitions,
                'sampling': 'deterministic random sampling without duplicate face groups within each operation type',
                'seed': args.seed,
                'three_plus_count': args.three_plus_count,
                'planned_operations': list(dict.fromkeys(plan[0] for plan in plans)),
            },
            'cases': cases,
            'summary': summarize(cases),
        }
        report_path = output_dir / 'benchmark.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(report_path), 'summary': report['summary']}, ensure_ascii=False, indent=2))
        return 0
    finally:
        if worker.is_running():
            worker.stop(timeout_seconds=60)


if __name__ == '__main__':
    raise SystemExit(main())
