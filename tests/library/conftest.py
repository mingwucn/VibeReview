from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from vibereview.library.models import LibraryConfig


def run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def synthetic_library(tmp_path: Path) -> tuple[Path, LibraryConfig, dict[str, bytes]]:
    superproject = tmp_path / "operator-source"
    superproject.mkdir()
    run_git(superproject, "init", "-b", "main")
    run_git(superproject, "config", "user.name", "Synthetic Fixture")
    run_git(superproject, "config", "user.email", "fixture.invalid@example.invalid")
    repository = superproject / "library"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Synthetic Fixture")
    run_git(repository, "config", "user.email", "fixture.invalid@example.invalid")

    papers = repository / "papers"
    papers.mkdir()
    paper_one = b"\\cite{Alpha2024}\n# Alpha\n\nA lattice signal changes under load.\n"
    paper_two = ("# Beta\n\n" + ("overflow " * 2200) + "\n").encode("utf-8")
    (papers / "Writer - 2024 - Alpha.md").write_bytes(paper_one)
    (papers / "Writer - 2023 - Beta.md").write_bytes(paper_two)
    bibliography = (
        "@article{Alpha2024,\n"
        " title={Synthetic Alpha Study},\n"
        " author={A. Example and B. Example},\n"
        " year={2024},\n"
        " doi={10.5555/synthetic.alpha},\n"
        " file={Writer - 2024 - Alpha.md}\n"
        "}\n"
    ).encode()
    (repository / "references.bib").write_bytes(bibliography)
    graph = json.dumps(
        {
            "nodes": [
                {
                    "id": "alpha-node",
                    "citekey": "Alpha2024",
                    "label": "lattice signal",
                    "attributes": {
                        "supports": [
                            {"evidence": "A lattice signal changes under load."}
                        ]
                    },
                }
            ],
            "edges": [],
        },
        sort_keys=True,
    ).encode()
    (repository / "graph.json").write_bytes(graph)
    (repository / "alternate-graph.json").write_bytes(graph)
    (repository / "README.md").write_text("# Synthetic library\n", encoding="utf-8")
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "synthetic corpus")
    commit = run_git(repository, "rev-parse", "HEAD")
    run_git(
        superproject,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},library",
    )
    run_git(superproject, "commit", "-m", "pin synthetic library")
    config = LibraryConfig(
        library_id="synthetic",
        library_path=repository,
        superproject_path=superproject,
        gitlink_path="library",
        expected_commit=commit,
        markdown_root="papers",
        bibliography="references.bib",
        graph_path="graph.json",
    )
    return repository, config, {
        "papers/Writer - 2024 - Alpha.md": paper_one,
        "papers/Writer - 2023 - Beta.md": paper_two,
        "references.bib": bibliography,
        "graph.json": graph,
    }
