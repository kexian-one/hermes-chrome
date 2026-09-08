from agent.ecom_modules import load_scripts
import pytest

load_scripts()


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False, help="Explicitly enable external model/browser smoke tests")


@pytest.fixture(autouse=True)
def offline_data_sources(request, monkeypatch):
    if not request.config.getoption("--run-live"):
        monkeypatch.setenv("ALL_IN_AI_OFFLINE", "1")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    for item in items:
        if item.path.name in {"test_llm_smoke.py", "test_mcp_connectivity.py"}:
            item.add_marker(pytest.mark.skip(reason="external services require --run-live"))


@pytest.fixture
def valid_sourcing_writer(monkeypatch):
    import json
    from pathlib import Path
    from agent.builtin_tools import execute_builtin
    from sourcing_pipeline import run_from_files
    def execute(name, args, root, **kwargs):
        result = execute_builtin(name, args, root, **kwargs)
        data = json.loads(args)
        if name == "write_file" and str(data.get("path", "")).endswith(".csv"):
            import os
            output = Path(os.environ["WORKER_OUTPUT_DIR"])
            source = output / ".ecom-scratch" / "fixture.json"
            source.parent.mkdir(exist_ok=True)
            source.write_text('{"target":{},"candidates":[]}', encoding="utf-8")
            run_from_files(jd_product_path=None, candidates_path=None, merged_input_path=str(source), output_path=str(output / data["path"]), confirm_details=False)
        return result
    monkeypatch.setattr("agent.worker.execute_builtin", execute)
