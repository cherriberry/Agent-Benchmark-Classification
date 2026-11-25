# Agent benchmark projects

Three projects are maintained as ordinary directories in this single Git repository. No submodules or nested Git repositories are required.

| Directory | Contents |
|---|---|
| [agent-classification](agent-classification/README.md) | Latest V5/r8 classification code, exact evaluated prompt, and summary statistics for Agent-SafetyBench and ToolEmu |
| [my-harbor](my-harbor/README.md) | Meeting-minutes Harbor task, environment, tools, checks, and existing local experiment records |
| [my-harbor-long](my-harbor-long/README.md) | Extended meeting task, verifiers, reference solution, model-matrix scripts, and existing analysis |

The Harbor directories were copied from `/data/fuyuhan/my-harbor` and `/data/fuyuhan/my-harbor-long` without modifying their source files. Generated Python bytecode caches were omitted. [packaging/source-copies.json](packaging/source-copies.json) records the SHA-256 of every copied Harbor file.

The classification project retains its original Git history. Older classifier variants remain accessible in that history; the current directory contains the latest classifier and its full/sample runners. Full classification datasets and API attempt logs remain outside Git. Latest classification counts and run provenance are included under `agent-classification/metadata/`.

## Running the projects

Follow each project's README. This upload preserves the existing Harbor scripts, including their original absolute paths and dependencies on the local HarnessSecurity-Benchmark installation. Uploading the directories does not make those dependencies available automatically, and does not rerun any model experiments.
