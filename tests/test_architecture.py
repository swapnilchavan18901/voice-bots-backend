"""Keep domain pipelines independent as features are added."""

import ast
from pathlib import Path


def test_pipeline_dependencies_do_not_cross_domains():
    ai = Path(__file__).resolve().parents[1] / "app" / "ai"
    domains = {"clinic", "meeting", "conversation"}
    for domain in domains | {"common"}:
        forbidden = domains - {domain}
        for path in (ai / domain).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    assert node.level == 0, f"Use explicit imports in {path}"
                    modules = [node.module or ""]
                for module in modules:
                    assert module != "app.ai.pipeline", path
                    assert not any(
                        module == f"app.ai.{other}" or module.startswith(f"app.ai.{other}.")
                        for other in forbidden
                    ), f"{path} imports another pipeline: {module}"
