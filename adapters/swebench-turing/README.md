# SWE-bench Turing → Harbor Adapter

## Overview

This adapter converts SWE-bench instances from **local JSON/JSONL files** (compatible with the SWE-bench harness fork) into **Harbor-compatible tasks**, enabling evaluation of reasoning agents within isolated, reproducible software environments.

This adapter is designed to work with datasets that follow the SWE-bench format but are stored locally (e.g., from the Microsoft SWE-Bench harness fork) rather than loaded from HuggingFace.

- **Benchmark type:** Software engineering and bug-fixing  
- **Language:** Python  
- **Data source:** Local `.json` or `.jsonl` files  
- **Adapter scope:** All repositories covered by SWE-bench format  

---

## What is SWE-bench?

SWE-bench is a benchmark designed to evaluate LLMs' ability to **read, understand, and fix real-world bugs** in open-source repositories.  
Each instance contains:
- A natural-language bug report or issue description
- The repository name and base commit
- A patch that resolves the issue (the "oracle")
- A set of tests that verify correctness

**Metric:** Accuracy is defined as the fraction of correctly fixed tasks verified via the benchmark's test harness.

---

## Adapter Features

- ✅ Loads from local JSON/JSONL files (supports both single object and array formats)
- ✅ Automatic Docker image management (leverages prebuilt SWE-bench images)
- ✅ Generates Harbor-compatible tasks from SWE-bench instances
- ✅ Reproducible environments with per-task Dockerfiles
- ✅ Auto-generated `instruction.md`, `task.toml`, and `tests/` scaffolds
- ✅ Support for both **Oracle** and **Agent-based** evaluation
- ✅ Compatible with both XML and JSON parsers for agents

---

## Generated Task Structure

```
datasets/swebench-turing/
├── {task_id}/
│   ├── task.toml
│   ├── instruction.md
│   ├── environment/
│   │   └── Dockerfile
│   ├── solution/
│   │   └── solve.sh
│   └── tests/
│       ├── test.sh
│       └── config.json
```

Each task corresponds to a single SWE-bench instance.  
The adapter template directory mirrors this layout under `harbor/adapters/swebench-turing/template/`.

---

## Usage: Create Task Directories

### Prerequisites

1. Install dependencies:
```bash
cd adapters/swebench-turing
uv sync  # or pip install -e .
```

2. Set the GitHub token environment variable:
```bash
export SWEBENCH_GITHUB_TOKEN="ghp_your_token_here"
```
   This token is used to clone the SWE-bench fork repository during test evaluation. The token must have access to the `TuringGpt/Microsoft-SWE-Bench` repository.

3. Prepare your dataset file:
   - Format: `.json` (single object, array of objects, or `.jsonl` (one JSON object per line)
   - Required fields: `instance_id`, `repo`, `base_commit`, `problem_statement`, `gold_patch` (or `patch`), `test_patch`

### Generate Tasks

```bash
cd adapters/swebench-turing

# Generate all instances from a JSON file
uv run run_adapter.py \
    --dataset-path /path/to/dataset.json \
    --task-dir ../../datasets/swebench-turing

# Generate all instances from a JSONL file
uv run run_adapter.py \
    --dataset-path /path/to/dataset.jsonl \
    --task-dir ../../datasets/swebench-turing

# Generate a single instance
uv run run_adapter.py \
    --dataset-path /path/to/dataset.json \
    --task-dir ../../datasets/swebench-turing \
    --instance-id ag2ai__ag2-2047

# Generate with limit
uv run run_adapter.py \
    --dataset-path /path/to/dataset.json \
    --task-dir ../../datasets/swebench-turing \
    --limit 10

# Overwrite existing tasks
uv run run_adapter.py \
    --dataset-path /path/to/dataset.json \
    --task-dir ../../datasets/swebench-turing \
    --overwrite
```

### Command-line Options

- `--dataset-path`: Path to dataset.json or dataset.jsonl file (required)
- `--task-dir`: Output directory for Harbor tasks (required)
- `--instance-id`: Convert a single instance (optional)
- `--all` / `--no-all`: Convert all instances (default: `--all`)
- `--limit`: Maximum number of instances to convert
- `--overwrite`: Overwrite existing task directories
- `--timeout`: Task timeout in seconds (default: 3000.0)
- `--template-dir`: Override template directory (optional)

---

## Run Evaluation / Harness in Harbor

You can evaluate agents on the generated SWE-bench tasks using Harbor's job system.

### Using Job Configurations

```bash
# From harbor root directory
uv run harbor jobs start \
    -c adapters/swebench-turing/swebench-turing.yaml \
    -a <agent_name> \
    -m "<model_name>"
```

Or without config:
```bash
uv run harbor jobs start \
    -p datasets/swebench-turing \
    -a <agent_name> \
    -m "<model_name>"
```

### Running Individual Trials

```bash
# Test with oracle agent (reference solution)
uv run harbor trials start \
    -p datasets/swebench-turing/<task_id> \
    -a oracle

# Test with your agent
uv run harbor trials start \
    -p datasets/swebench-turing/<task_id> \
    -a <agent> \
    -m "<model>"
```

Results will appear under `jobs/` or `trials/` by default.

---

## Dataset Format

The adapter supports both JSON and JSONL formats:

### JSON Format

**Single object:**
```json
{
  "instance_id": "ag2ai__ag2-2047",
  "repo": "ag2ai/ag2",
  "base_commit": "6bec0df6c4f1299ef5a33ace97a08d46b2e80749",
  "problem_statement": "...",
  "gold_patch": "diff --git ...",
  "test_patch": "diff --git ...",
  "difficulty": "easy"
}
```

**Array of objects:**
```json
[
  { "instance_id": "...", ... },
  { "instance_id": "...", ... }
]
```

### JSONL Format

One JSON object per line:
```jsonl
{"instance_id": "ag2ai__ag2-2047", "repo": "ag2ai/ag2", ...}
{"instance_id": "django__django-13741", "repo": "django/django", ...}
```

### Field Mapping

The adapter maps fields as follows:
- `instance_id` → `instance_id`
- `repo` → `repo`
- `base_commit` → `base_commit`
- `problem_statement` → `problem_statement`
- `gold_patch` or `patch` → `patch` (for solution)
- `test_patch` → `test_patch`
- `difficulty` → `difficulty` (defaults to "hard" if missing)
- `version` → extracted from `instance_id` if not present

---

## Differences from `swebench` Adapter

| Aspect | `swebench` | `swebench-turing` |
|--------|------------|-------------------|
| Data Source | HuggingFace `princeton-nlp/SWE-bench_Verified` | Local `.json`/`.jsonl` file |
| Loader | `SWEBenchLoader` (HF datasets) | `SWEBenchTuringLoader` (local files) |
| Use Case | Standard SWE-bench Verified dataset | Custom datasets, harness forks |

Both adapters generate identical Harbor task structures and use the same evaluation logic.

---

## Troubleshooting

### Docker Image Issues

If you encounter Docker image errors, ensure that:
1. The SWE-bench harness package is installed (`swebench>=4.1.0`)
2. Docker images can be built or are available for your instances
3. The `instance_id` format matches what the harness expects

### Missing Fields

If records are missing required fields, the adapter will raise a `ValueError`. Ensure your dataset includes:
- `instance_id`
- `repo`
- `base_commit`
- `problem_statement`
- `gold_patch` or `patch`
- `test_patch`

### GitHub Token Issues

If you encounter errors about `SWEBENCH_GITHUB_TOKEN`:
1. Ensure the environment variable is set: `export SWEBENCH_GITHUB_TOKEN="ghp_..."`
2. Verify the token has access to the `TuringGpt/Microsoft-SWE-Bench` repository
3. The token is only used during task generation (embedded in the test script), not during runtime evaluation

---

## License

This adapter follows the same licensing as the Harbor framework and SWE-bench benchmark.
