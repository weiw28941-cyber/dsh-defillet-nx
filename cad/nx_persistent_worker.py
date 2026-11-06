# -*- coding: utf-8 -*-
"""Long-lived NX2406 journal worker using an atomic filesystem queue."""

from __future__ import print_function

import argparse
import hashlib
import io
import json
import math
import os
import sys
import time
import traceback

import NXOpen
import NXOpen.UF

PROTOCOL_VERSION = 1
WORKER_BUILD = "nx2406-persistent-journal-v2-normalized-input"
MAX_CODE_CHARS = 200000
MAX_OUTPUT_CHARS = 16000
POLL_SECONDS = 0.10


class OperationError(Exception):
    def __init__(self, code, message, phase, retry_class="never", details=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.phase = phase
        self.retry_class = retry_class
        self.details = details or {}


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_json(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except Exception:
            pass
    last_error = None
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.01 * (attempt + 1))
    raise last_error


def read_json(path):
    with open(path, "r") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise OperationError("NX_INVALID_REQUEST", "Request must be a JSON object", "protocol")
    return value


def code_hash(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def bounded(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n... (truncated)", True


def jsonable(value, depth=0):
    if depth > 10:
        raise OperationError("NX_RESULT_NOT_SERIALIZABLE", "Result nesting is too deep", "serialize")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item, depth + 1) for key, item in value.items()}
    raise OperationError(
        "NX_RESULT_NOT_SERIALIZABLE",
        "Result contains unsupported type %s" % type(value).__name__,
        "serialize",
    )


def error_payload(error, operation_id=None):
    if isinstance(error, OperationError):
        payload = {
            "code": error.code,
            "message": error.message,
            "phase": error.phase,
            "retry_class": error.retry_class,
            "details": error.details,
        }
    else:
        payload = {
            "code": "NX_API_ERROR",
            "message": str(error) or type(error).__name__,
            "phase": "execute",
            "retry_class": "never",
            "details": {"traceback": traceback.format_exc()[-12000:]},
        }
        nx_code = getattr(error, "ErrorCode", None)
        if nx_code is None:
            nx_code = getattr(error, "nx_error_code", None)
        if nx_code is not None:
            payload["nx_code"] = nx_code
        deletion_seconds = getattr(error, "deletion_seconds", None)
        if deletion_seconds is not None:
            payload["details"]["deletion_seconds"] = float(deletion_seconds)
        topology_change = getattr(error, "topology_change", None)
        if topology_change is not None:
            payload["details"]["topology_change"] = jsonable(topology_change)
        deletion_attempt = getattr(error, "deletion_attempt", None)
        if deletion_attempt is not None:
            payload["details"]["deletion_attempt"] = jsonable(deletion_attempt)
    if operation_id:
        payload["operation_id"] = operation_id
    return payload


def collection_count(collection):
    return sum(1 for _item in collection)


def part_identity(part):
    return "%s:%s" % (int(getattr(part, "Tag", 0)), getattr(part, "FullPath", "") or part.Name)


def rounded_values(values):
    return [round(float(value), 9) for value in values]


def body_mass_properties(part, body):
    units = list(part.UnitCollection)
    area_unit = next(unit for unit in units if str(unit.Name).lower() in ("squaremillimeter", "square millimeter"))
    volume_unit = next(unit for unit in units if str(unit.Name).lower() in ("cubicmillimeter", "cubic millimeter"))
    mass_unit = next(unit for unit in units if str(unit.Name).lower() in ("kilogram", "gram"))
    length_unit = next(unit for unit in units if str(unit.Name).lower() in ("millimeter", "millimeters"))
    force_unit = next(unit for unit in units if "newton" in str(unit.Name).lower())
    measure = part.MeasureManager.NewMassProperties(
        [area_unit, volume_unit, mass_unit, length_unit, force_unit],
        0.999,
        [body],
    )
    centroid = measure.Centroid
    return {
        "area": round(float(measure.Area), 9),
        "volume": round(float(measure.Volume), 9),
        "centroid": rounded_values((centroid.X, centroid.Y, centroid.Z)),
    }


def body_geometry_points(body):
    points = []
    for edge in body.GetEdges():
        try:
            first, second = edge.GetVertices()
        except Exception:
            continue
        for point in (first, second):
            if point is not None:
                points.append((float(point.X), float(point.Y), float(point.Z)))
    return points


def body_bounding_box(body):
    points = body_geometry_points(body)
    if not points:
        return None
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]
    lengths = [maximum[axis] - minimum[axis] for axis in range(3)]
    return {
        "minimum": rounded_values(minimum),
        "maximum": rounded_values(maximum),
        "lengths": rounded_values(lengths),
        "vertex_checksum": rounded_values(
            [sum(point[axis] for point in points) for axis in range(3)]
        ),
        "vertex_sample_count": len(points),
    }


def model_signature(part, display_part=None):
    bodies = []
    for body in part.Bodies:
        try:
            face_count = len(body.GetFaces())
        except Exception:
            face_count = -1
        try:
            edge_count = len(body.GetEdges())
        except Exception:
            edge_count = -1
        row = {
            "tag": int(getattr(body, "Tag", 0)),
            "solid": bool(getattr(body, "IsSolidBody", False)),
            "face_count": face_count,
            "edge_count": edge_count,
        }
        if row["solid"]:
            try:
                row["mass_properties"] = body_mass_properties(part, body)
            except Exception as exc:
                row["mass_properties_error"] = str(exc)
        try:
            row["bounding_box"] = body_bounding_box(body)
        except Exception as exc:
            row["bounding_box_error"] = str(exc)
        bodies.append(row)
    bodies.sort(key=lambda item: item["tag"])
    return {
        "part_identity": part_identity(part),
        "display_part_identity": None if display_part is None else part_identity(display_part),
        "body_count": len(bodies),
        "feature_count": collection_count(part.Features),
        "bodies": bodies,
    }


def _within_tolerance(left, right, relative=1e-4, absolute=1e-5):
    try:
        left = float(left)
        right = float(right)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(left) or not math.isfinite(right):
        return False
    return abs(left - right) <= max(
        absolute, relative * max(abs(left), abs(right), 1.0)
    )


UNDO_MASS_PROPERTY_TOLERANCES = {
    "area": (1e-8, 2e-5),
    "volume": (1e-8, 2e-4),
    "centroid": (1e-9, 1e-6),
}


def _undo_mass_properties_match(baseline, restored):
    if set(baseline) != set(restored):
        return False
    for key, value in baseline.items():
        restored_value = restored[key]
        if key == "centroid":
            if not isinstance(value, (list, tuple)) or not isinstance(restored_value, (list, tuple)):
                return False
            if len(value) != len(restored_value):
                return False
            relative, absolute = UNDO_MASS_PROPERTY_TOLERANCES[key]
            if any(
                not _within_tolerance(left, right, relative=relative, absolute=absolute)
                for left, right in zip(value, restored_value)
            ):
                return False
        elif key in UNDO_MASS_PROPERTY_TOLERANCES:
            relative, absolute = UNDO_MASS_PROPERTY_TOLERANCES[key]
            if not _within_tolerance(value, restored_value, relative=relative, absolute=absolute):
                return False
        elif value != restored_value:
            return False
    return True


def undo_signatures_match(baseline, restored):
    """Compare an Undo result without treating measured float drift as topology change."""
    if not isinstance(baseline, dict) or not isinstance(restored, dict):
        return False
    if set(baseline) != set(restored):
        return False
    for key, value in baseline.items():
        restored_value = restored[key]
        if key != "bodies":
            if value != restored_value:
                return False
            continue
        if not isinstance(value, list) or not isinstance(restored_value, list):
            return False
        if len(value) != len(restored_value):
            return False
        for baseline_body, restored_body in zip(value, restored_value):
            if not isinstance(baseline_body, dict) or not isinstance(restored_body, dict):
                return False
            if set(baseline_body) != set(restored_body):
                return False
            for body_key, body_value in baseline_body.items():
                restored_body_value = restored_body[body_key]
                if body_key == "mass_properties":
                    if not _undo_mass_properties_match(body_value, restored_body_value):
                        return False
                elif body_value != restored_body_value:
                    return False
    return True


def export_normalized_step(session, part, output_path, settings_file):
    output_path = os.path.abspath(output_path)
    settings_file = os.path.abspath(settings_file)
    if not os.path.isfile(settings_file):
        raise OperationError("NX_NORMALIZATION_FAILED", "STEP settings file is missing", "normalization")
    parent = os.path.dirname(output_path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    if os.path.exists(output_path):
        os.remove(output_path)
    creator = session.DexManager.CreateStepCreator()
    try:
        creator.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
        creator.SettingsFile = settings_file
        creator.ObjectTypes.Solids = True
        creator.InputFile = part.FullPath
        creator.OutputFile = output_path
        creator.FileSaveFlag = False
        creator.LayerMask = "1-256"
        creator.ProcessHoldFlag = True
        creator.Commit()
    finally:
        creator.Destroy()
    if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
        raise OperationError("NX_NORMALIZATION_FAILED", "NX produced no normalized STEP output", "normalization")
    return output_path


def close_all_parts(session):
    parts = []
    for part in session.Parts:
        parts.append(part)
    for part in parts:
        try:
            part.Close(
                NXOpen.BasePart.CloseWholeTree.TrueValue,
                NXOpen.BasePart.CloseModified.CloseModified,
                None,
            )
        except Exception:
            pass
    try:
        session.ApplicationSwitchImmediate("UG_APP_NOPART")
    except Exception:
        pass


def load_step(session, input_path):
    if not os.path.isfile(input_path):
        raise OperationError("NX_INVALID_REQUEST", "Input STEP does not exist", "load")
    close_all_parts(session)
    part, status = session.Parts.OpenActiveDisplay(
        input_path, NXOpen.DisplayPartOption.AllowAdditional
    )
    try:
        session.ApplicationSwitchImmediate("UG_APP_MODELING")
        work_part = session.Parts.Work
        if work_part is None:
            raise OperationError("NX_WORKER_UNAVAILABLE", "NX did not create a Work Part", "load")
        return work_part
    finally:
        status.Dispose()


def normalize_loaded_step(session, part, workspace):
    """Round-trip the imported part and expose only the re-imported result."""
    settings_file = os.environ.get("NX_STEP214_EXPORT_DEF") or os.path.join(
        os.environ.get("UGII_BASE_DIR") or os.path.join(os.environ.get("ProgramFiles", "C:/Program Files"), "Siemens", "NX2406"),
        "STEP214UG", "ugstep214.def",
    )
    normalized_path = os.path.join(workspace, "input", "normalized.stp")
    export_normalized_step(session, part, normalized_path, settings_file)
    close_all_parts(session)
    normalized_part = load_step(session, normalized_path)
    normalized_signature = model_signature(normalized_part, session.Parts.Display)
    return normalized_part, normalized_path, {
        "normalized_signature": normalized_signature,
        "normalized_path": normalized_path,
        "reimported": True,
    }


def normalization_summary(normalized_signature):
    return {
        "normalized_face_count": sum(
            body.get("face_count", 0) for body in normalized_signature.get("bodies", [])
        ),
        "normalized_body_count": len(normalized_signature.get("bodies", [])),
        "reimported": True,
    }


def execute_code(code, session, part, runtime_state):
    namespace = {
        "__name__": "__nx_persistent_operation__",
        "__doc__": None,
        "NXOpen": NXOpen,
        "session": session,
        "workPart": part,
        "displayPart": session.Parts.Display,
    }
    namespace["ctx"] = {
        "session": session,
        "work_part": part,
        "display_part": session.Parts.Display,
        "runtime_state": runtime_state,
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    result = None
    try:
        sys.stdout, sys.stderr = stdout, stderr
        try:
            result = eval(compile(code, "<nx-persistent-worker>", "eval"), namespace, namespace)
        except SyntaxError:
            exec(compile(code, "<nx-persistent-worker>", "exec"), namespace, namespace)
            result = namespace.get("result")
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
    out, out_cut = bounded(stdout.getvalue())
    err, err_cut = bounded(stderr.getvalue())
    return jsonable(result), out, err, out_cut or err_cut


class Worker(object):
    def __init__(self, workspace, input_step):
        self.workspace = os.path.abspath(workspace)
        self.requests = os.path.join(self.workspace, "requests")
        self.running = os.path.join(self.workspace, "running")
        self.results = os.path.join(self.workspace, "results")
        self.control = os.path.join(self.workspace, "control.json")
        self.heartbeat = os.path.join(self.workspace, "heartbeat.json")
        for path in (self.workspace, self.requests, self.running, self.results):
            if not os.path.isdir(path):
                os.makedirs(path)
        self.session = NXOpen.Session.GetSession()
        self.part = load_step(self.session, os.path.abspath(input_step))
        self.part, self.normalized_input, self.normalization = normalize_loaded_step(
            self.session, self.part, self.workspace
        )
        self.part_identity = part_identity(self.part)
        self.runtime_state = 0
        self.quarantined = False
        self.active_operation = None
        self.operations = {}
        self.commit_checkpoints = []
        self.started_at = utc_now()

    def heartbeat_payload(self, status="ready"):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "worker_build": WORKER_BUILD,
            "pid": os.getpid(),
            "status": "quarantined" if self.quarantined else status,
            "runtime_state": self.runtime_state,
            "part_identity": self.part_identity,
            "work_part": self.part.Name,
            "work_part_path": self.part.FullPath,
            "normalization": normalization_summary(
                self.normalization["normalized_signature"],
            ),
            "active_operation_id": self.active_operation,
            "started_at": self.started_at,
            "updated_at": utc_now(),
        }

    def write_heartbeat(self, status="ready"):
        atomic_json(self.heartbeat, self.heartbeat_payload(status))

    def should_stop(self):
        if not os.path.isfile(self.control):
            return False
        try:
            return read_json(self.control).get("command") == "shutdown"
        except Exception:
            return False

    def claim_next(self):
        names = sorted(
            name for name in os.listdir(self.requests) if name.endswith(".request.json")
        )
        for name in names:
            source = os.path.join(self.requests, name)
            target = os.path.join(self.running, name)
            try:
                os.rename(source, target)
                return target
            except OSError:
                continue
        return None

    def validate_request(self, request):
        allowed = {
            "protocol_version", "operation_id", "base_state", "mode", "code",
            "code_hash", "preview_operation_id", "deadline_at", "submitted_at",
            "action", "output_path", "settings_file", "rollback_operation_id"
        }
        extras = set(request) - allowed
        if extras:
            raise OperationError(
                "NX_INVALID_REQUEST", "Unknown request fields", "protocol", details={"fields": sorted(extras)}
            )
        if request.get("protocol_version") != PROTOCOL_VERSION:
            raise OperationError(
                "NX_PROTOCOL_VERSION_MISMATCH", "Protocol version does not match", "protocol"
            )
        operation_id = request.get("operation_id")
        mode = request.get("mode")
        code = request.get("code")
        action = request.get("action", "execute")
        if not isinstance(operation_id, str) or not operation_id:
            raise OperationError("NX_INVALID_REQUEST", "operation_id is required", "protocol")
        if action not in ("execute", "export_step", "rollback_latest"):
            raise OperationError("NX_INVALID_REQUEST", "action is invalid", "protocol")
        if action == "rollback_latest":
            if mode != "commit":
                raise OperationError("NX_INVALID_REQUEST", "rollback_latest must use commit mode", "protocol")
            code = "__trusted_rollback_latest__"
        elif action == "export_step":
            if mode != "read":
                raise OperationError("NX_INVALID_REQUEST", "export_step must use read mode", "protocol")
            if request.get("output_path") is None or request.get("settings_file") is None:
                raise OperationError("NX_INVALID_REQUEST", "export_step paths are required", "protocol")
            code = "__trusted_export_step__"
        elif mode not in ("read", "preview", "commit"):
            raise OperationError("NX_INVALID_REQUEST", "mode is invalid", "protocol")
        if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE_CHARS:
            raise OperationError("NX_CODE_INVALID", "code is empty or too large", "protocol")
        if request.get("code_hash") != code_hash(code):
            raise OperationError("NX_CODE_INVALID", "code_hash does not match code", "protocol")
        if request.get("base_state") != self.runtime_state:
            raise OperationError(
                "NX_STATE_CONFLICT",
                "Requested base state is no longer current",
                "preflight",
                details={"requested": request.get("base_state"), "current": self.runtime_state},
            )
        if action == "rollback_latest":
            expected = request.get("rollback_operation_id")
            if not self.commit_checkpoints:
                raise OperationError("NX_NO_COMMIT_CHECKPOINT", "No retained commit can be rolled back", "preflight")
            if self.commit_checkpoints[-1]["operation_id"] != expected:
                raise OperationError(
                    "NX_ROLLBACK_NOT_LATEST",
                    "Only the latest retained commit may be rolled back",
                    "preflight",
                    details={"latest_operation_id": self.commit_checkpoints[-1]["operation_id"]},
                )
        if self.quarantined:
            raise OperationError(
                "NX_RUNTIME_QUARANTINED", "Worker requires reset", "preflight", "reset_required"
            )
        deadline = request.get("deadline_at")
        if deadline is not None and float(deadline) <= time.time():
            raise OperationError(
                "NX_DEADLINE_EXPIRED", "Request expired before execution", "queue", "never"
            )
        return operation_id, mode, code, action

    def trusted_export_step(self, output_path, settings_file):
        output_path = os.path.abspath(output_path)
        settings_file = os.path.abspath(settings_file)
        if not os.path.isfile(settings_file):
            raise OperationError("NX_EXPORT_FAILED", "STEP settings file is missing", "export")
        parent = os.path.dirname(output_path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        if os.path.exists(output_path):
            os.remove(output_path)
        creator = self.session.DexManager.CreateStepCreator()
        try:
            creator.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
            creator.SettingsFile = settings_file
            creator.ObjectTypes.Solids = True
            creator.InputFile = self.part.FullPath
            creator.OutputFile = output_path
            creator.FileSaveFlag = False
            creator.LayerMask = "1-256"
            creator.ProcessHoldFlag = True
            creator.Commit()
        finally:
            creator.Destroy()
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise OperationError("NX_EXPORT_EMPTY", "NX produced no STEP output", "export")
        return {"output_path": output_path, "size": os.path.getsize(output_path)}

    def undo(self, mark, baseline):
        try:
            self.session.UndoToMark(mark, None)
            self.part = self.session.Parts.Work
            restored = model_signature(self.part, self.session.Parts.Display)
        except Exception as exc:
            self.quarantined = True
            raise OperationError(
                "NX_ROLLBACK_FAILED", "Undo failed: %s" % exc, "rollback", "reset_required"
            )
        if not undo_signatures_match(baseline, restored):
            self.quarantined = True
            raise OperationError(
                "NX_ROLLBACK_FAILED",
                "Undo did not restore the baseline signature",
                "rollback_validation",
                "reset_required",
                {"baseline": baseline, "restore_state": restored},
            )
        return restored

    def execute(self, request):
        operation_id = request.get("operation_id")
        immutable = (
            request.get("base_state"), request.get("mode"), request.get("code_hash"),
            request.get("preview_operation_id"), request.get("action", "execute"),
            request.get("rollback_operation_id")
        )
        existing = self.operations.get(operation_id)
        if existing is not None:
            if tuple(existing.get("immutable") or ()) != immutable:
                raise OperationError(
                    "NX_OPERATION_ID_CONFLICT", "operation_id is already in use", "preflight"
                )
            return dict(existing)
        operation_id, mode, code, action = self.validate_request(request)
        operation_started = time.perf_counter()
        timings = {}
        self.active_operation = operation_id
        self.write_heartbeat("busy")
        phase_started = time.perf_counter()
        baseline = model_signature(self.part, self.session.Parts.Display)
        timings["baseline_signature_seconds"] = time.perf_counter() - phase_started
        if action == "rollback_latest":
            checkpoint = self.commit_checkpoints[-1]
            record = {
                "protocol_version": PROTOCOL_VERSION,
                "operation_id": operation_id,
                "immutable": immutable,
                "mode": mode,
                "state": "running",
                "base_state": self.runtime_state,
                "result_state": self.runtime_state,
                "code_hash": request["code_hash"],
                "retained": False,
                "rolled_back": False,
                "baseline": baseline,
                "received_at": utc_now(),
                "started_at": utc_now(),
            }
            self.operations[operation_id] = record
            try:
                phase_started = time.perf_counter()
                restored = self.undo(checkpoint["mark"], checkpoint["baseline"])
                timings["rollback_seconds"] = time.perf_counter() - phase_started
                self.commit_checkpoints.pop()
                try:
                    self.session.DeleteUndoMark(checkpoint["mark"], None)
                except Exception:
                    pass
                self.runtime_state += 1
                record.update(
                    state="committed", retained=True, result_state=self.runtime_state,
                    result={
                        "rolled_back_operation_id": checkpoint["operation_id"],
                        "runtime_state": self.runtime_state,
                    },
                    post_state=restored,
                    validation={"restored_checkpoint": True},
                    rolled_back_operation_id=checkpoint["operation_id"],
                    invalidated_reference_scopes=["topology"],
                )
            except Exception as exc:
                self.quarantined = True
                record.update(state="rollback_failed", error=error_payload(exc, operation_id))
            record["completed_at"] = utc_now()
            timings["total_seconds"] = time.perf_counter() - operation_started
            record["timings"] = dict(
                (name, round(float(value), 9)) for name, value in timings.items()
            )
            self.active_operation = None
            self.write_heartbeat("ready")
            return dict(record)
        phase_started = time.perf_counter()
        mark = self.session.SetUndoMark(self.session.MarkVisibility.Visible, "runtime op")
        timings["set_undo_mark_seconds"] = time.perf_counter() - phase_started
        record = {
            "protocol_version": PROTOCOL_VERSION,
            "operation_id": operation_id,
            "immutable": immutable,
            "mode": mode,
            "state": "running",
            "base_state": self.runtime_state,
            "result_state": self.runtime_state,
            "code_hash": request["code_hash"],
            "retained": False,
            "rolled_back": False,
            "baseline": baseline,
            "received_at": utc_now(),
            "started_at": utc_now(),
        }
        self.operations[operation_id] = record
        try:
            phase_started = time.perf_counter()
            try:
                if action == "export_step":
                    result = self.trusted_export_step(
                        request["output_path"], request["settings_file"]
                    )
                    stdout, stderr, truncated = "", "", False
                else:
                    result, stdout, stderr, truncated = execute_code(
                        code, self.session, self.part, self.runtime_state
                    )
            finally:
                timings["execute_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            update_errors = int(self.session.UpdateManager.DoUpdate(mark))
            timings["update_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            post_state = model_signature(
                self.session.Parts.Work, self.session.Parts.Display
            )
            timings["post_signature_seconds"] = time.perf_counter() - phase_started
            record.update(
                result=result,
                stdout=stdout,
                stderr=stderr,
                output_truncated=truncated,
                post_state=post_state,
                validation={"nx_update_error_count": update_errors},
            )
            if update_errors:
                raise OperationError(
                    "NX_UPDATE_FAILED", "NX reported update errors", "update",
                    details={"count": update_errors}
                )
            changed = post_state != baseline
            if mode == "read" and changed and action != "export_step":
                phase_started = time.perf_counter()
                restored = self.undo(mark, baseline)
                timings["rollback_seconds"] = time.perf_counter() - phase_started
                record.update(
                    state="rolled_back", rolled_back=True, restore_state=restored,
                    error=error_payload(OperationError(
                        "NX_READ_MUTATED_MODEL", "Read operation modified the model", "validation"
                    ), operation_id)
                )
            elif mode == "read":
                record["state"] = "completed"
            elif mode == "preview":
                phase_started = time.perf_counter()
                restored = self.undo(mark, baseline)
                timings["rollback_seconds"] = time.perf_counter() - phase_started
                record.update(
                    state="rolled_back", rolled_back=True, restore_state=restored,
                    invalidated_reference_scopes=["topology"]
                )
            else:
                self.runtime_state += 1
                self.commit_checkpoints.append({
                    "operation_id": operation_id,
                    "mark": mark,
                    "baseline": baseline,
                })
                record.update(
                    state="committed", retained=True, result_state=self.runtime_state,
                    invalidated_reference_scopes=["topology"]
                )
            if action == "export_step" and changed:
                phase_started = time.perf_counter()
                restored = self.undo(mark, baseline)
                timings["rollback_seconds"] = time.perf_counter() - phase_started
                self.quarantined = True
                record.update(
                    state="rollback_failed", rolled_back=True, restore_state=restored,
                    error=error_payload(OperationError(
                        "NX_EXPORT_MUTATED_MODEL",
                        "Trusted STEP export unexpectedly changed the live model",
                        "export_validation",
                        "reset_required",
                    ), operation_id),
                )
        except Exception as exc:
            try:
                phase_started = time.perf_counter()
                restored = self.undo(mark, baseline)
                timings["rollback_seconds"] = time.perf_counter() - phase_started
                record.update(
                    state="rolled_back", rolled_back=True, restore_state=restored,
                    error=error_payload(exc, operation_id),
                    invalidated_reference_scopes=["topology"]
                )
            except Exception as rollback_error:
                record.update(
                    state="rollback_failed", error=error_payload(rollback_error, operation_id)
                )
        record["completed_at"] = utc_now()
        timings["total_seconds"] = time.perf_counter() - operation_started
        record["timings"] = dict(
            (name, round(float(value), 9)) for name, value in timings.items()
        )
        if not (record.get("state") == "committed" and action == "execute"):
            try:
                self.session.DeleteUndoMark(mark, None)
            except Exception:
                pass
        self.active_operation = None
        self.write_heartbeat("ready")
        return dict(record)

    def process_request(self, path):
        operation_id = os.path.basename(path).split(".request.json", 1)[0]
        output = os.path.join(self.results, operation_id + ".result.json")
        try:
            request = read_json(path)
            result = self.execute(request)
        except Exception as exc:
            result = {
                "protocol_version": PROTOCOL_VERSION,
                "operation_id": operation_id,
                "state": "rejected",
                "retained": False,
                "rolled_back": False,
                "error": error_payload(exc, operation_id),
                "completed_at": utc_now(),
            }
        atomic_json(output, result)
        try:
            os.remove(path)
        except OSError:
            pass

    def run(self):
        self.write_heartbeat("ready")
        while not self.should_stop():
            request = self.claim_next()
            if request is None:
                self.write_heartbeat("ready")
                time.sleep(POLL_SECONDS)
                continue
            self.process_request(request)
        self.write_heartbeat("stopped")


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--input", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_args(sys.argv[1:] if argv is None else argv)
    Worker(arguments.workspace, arguments.input).run()


if __name__ == "__main__":
    main()
