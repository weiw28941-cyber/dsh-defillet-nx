"""Check packaged Python dependencies and tarball integrity without NXOpen."""

import ast
import hashlib
import json
import tarfile
from pathlib import Path


def check_runtime_closure(stage, allowed):
    """Check local imports and statically specified REPO_ROOT journal paths."""
    allowed = set(allowed)

    def require_file(path, source, dependency):
        relative = path.relative_to(stage).as_posix()
        assert relative in allowed and path.is_file(), (source, dependency, relative)

    def require_module(targets, source, dependency):
        for target in targets:
            for candidate in (target.with_suffix(".py"), target / "__init__.py"):
                if candidate.is_file():
                    require_file(candidate, source, dependency)
                    return
        raise AssertionError((source, dependency, "module not found"))

    def repo_path(expression):
        parts = []
        while isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Div):
            if not isinstance(expression.right, ast.Constant) or not isinstance(expression.right.value, str):
                return None
            parts.append(expression.right.value)
            expression = expression.left
        if isinstance(expression, ast.Name) and expression.id == "REPO_ROOT":
            return stage.joinpath(*reversed(parts))
        return None

    for relative in allowed:
        path = stage / relative
        require_file(path, relative, relative)
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
        search_roots = [path.parent, stage, stage / "webapp" / "backend"]
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Attribute)
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "sys" and node.func.value.attr == "path"
                    and node.func.attr in ("insert", "append") and node.args):
                value = node.args[-1]
                if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                        and value.func.id == "str" and len(value.args) == 1):
                    value = value.args[0]
                root = repo_path(value)
                if root is not None:
                    if node.func.attr == "insert":
                        search_roots.insert(0, root)
                    else:
                        search_roots.append(root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    base = path.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    if module:
                        require_module([base.joinpath(*module.split("."))], relative, module)
                    else:
                        for alias in node.names:
                            require_module([base / alias.name], relative, alias.name)
                elif module.startswith(("cad.", "OccUtils.", "app.", "nx_")):
                    require_module([root.joinpath(*module.split(".")) for root in search_roots], relative, module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if module.startswith(("cad.", "OccUtils.", "app.", "nx_")):
                        require_module([root.joinpath(*module.split(".")) for root in search_roots], relative, module)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                target = repo_path(node)
                if target is not None and target.suffix == ".py":
                    require_file(target, relative, target.relative_to(stage).as_posix())


def main():
    plugin = Path(__file__).resolve().parents[1]
    stage = plugin / "dist" / "package" / "runtime"
    manifest = json.loads((plugin / "release-files.json").read_text(encoding="utf-8"))
    check_runtime_closure(stage, manifest["runtime"])
    print("Runtime local import and journal closure verified:", len(manifest["runtime"]), "allowlisted files")

    release = json.loads((plugin / "dist/package/release-manifest.json").read_text(encoding="utf-8"))
    assert release["runtimeAllowlist"] == manifest["runtime"], "staged runtime allowlist is stale"
    artifact = plugin / "dist" / f"defeature-dsh-ui-{release['version']}.tgz"
    with tarfile.open(artifact, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
        expected = {"package/" + row["path"] for row in release["files"]}
        assert names == expected | {"package/release-manifest.json"}, names ^ expected
        for row in release["files"]:
            data = archive.extractfile("package/" + row["path"]).read()
            assert len(data) == row["bytes"], row["path"]
            assert hashlib.sha256(data).hexdigest() == row["sha256"], row["path"]
    print("Tarball contents and hashes verified:", len(names), "files")


if __name__ == "__main__":
    main()
