# SWE-bench Turing Adapter Architecture

This document explains the key code pointers and architecture of the `swebench-turing` adapter, which converts SWE-bench tasks from local JSON/JSONL files into Harbor's standardized task format.

## Table of Contents

1. [Overview](#overview)
2. [How It Uses Your SWE-bench Fork](#how-it-uses-your-swebench-fork)
3. [Input Transformation Pipeline](#input-transformation-pipeline)
4. [Docker Image Resolution](#docker-image-resolution)
5. [Parser Extraction Logic](#parser-extraction-logic)
6. [Key Code Locations](#key-code-locations)

---

## Overview

The `swebench-turing` adapter is designed to load SWE-bench tasks from local dataset files (`.json` or `.jsonl`) and convert them into Harbor's task directory structure. Unlike the standard `swebench` adapter that loads from HuggingFace datasets, this adapter:

- Reads from local files
- Supports both single-object and array JSON formats
- Uses your custom SWE-bench fork for evaluation
- Handles custom datasets that may not be in `MAP_REPO_VERSION_TO_SPECS`

---

## How It Uses Your SWE-bench Fork

### Location: `template/test.sh` (lines 6-14, 24)

The adapter uses your SWE-bench fork in the **parser script** that runs inside the container during test evaluation.

**Key Code:**
```python
# template/test.sh, lines 9-10
dependencies = [
    "swebench @ git+https://{GITHUB_TOKEN}@github.com/TuringGpt/Microsoft-SWE-Bench.git",
    ...
]
```

**How it works:**
1. The `test.sh` template contains an embedded Python script (`parser.py`) that gets written to disk at runtime
2. This script uses `uv`'s PEP 723 inline script dependencies to install your fork directly from GitHub
3. The GitHub token is embedded in the URL to allow cloning private repositories
4. The parser imports `LANGUAGE_PARSER_MAP` from your fork:
   ```python
   # template/test.sh, line 24
   from swebench.harness.log_parsers import LANGUAGE_PARSER_MAP
   ```

**Why this approach:**
- Ensures the parser uses the exact version of your fork with `LANGUAGE_PARSER_MAP`
- Avoids version mismatches between the adapter's local environment and the container
- Allows you to customize the evaluation logic in your fork without modifying Harbor

---

## Input Transformation Pipeline

### Entry Point: `adapter.py` → `SWEBenchTuringLoader`

**Location:** `adapter.py` lines 54-119

The input transformation happens in three stages:

#### Stage 1: File Loading (`SWEBenchTuringLoader._load_data`)

**Location:** `adapter.py` lines 70-99

```python
def _load_data(self) -> None:
    """Load data from JSON or JSONL file."""
    if self.dataset_path.suffix == ".json":
        # Handles both single object and array formats
        data = json.load(f)
        if isinstance(data, dict):
            data = [data]  # Normalize to list
    elif self.dataset_path.suffix == ".jsonl":
        # Read line-by-line
        for line in f:
            data.append(json.loads(line))
```

**Key features:**
- Supports both `.json` (single object or array) and `.jsonl` (one JSON object per line)
- Indexes all records by `instance_id` for fast lookup
- Stores raw records in `self._by_id` dictionary

#### Stage 2: Record Normalization (`SWEBenchRecord.from_dict`)

**Location:** `adapter.py` lines 26-51

```python
@classmethod
def from_dict(cls, d: dict) -> "SWEBenchRecord":
    # Handle harness format: gold_patch -> patch
    patch = d.get("patch") or d.get("gold_patch")
    
    # Extract version from instance_id if not present
    version = d.get("version")
    if not version and "instance_id" in d:
        parts = d["instance_id"].split("-")
        if len(parts) > 1:
            version = parts[-1]  # Extract from "repo__repo-version"
```

**Transformations:**
- Maps `gold_patch` → `patch` (handles both SWE-bench harness and Harbor formats)
- Extracts `version` from `instance_id` if missing (format: `repo__repo-version`)
- Normalizes field names and provides defaults

#### Stage 3: Harbor Task Generation (`SWEBenchTuringToHarbor.generate_task`)

**Location:** `adapter.py` lines 205-279

The adapter generates Harbor's standardized task structure:

```
task_dir/
├── instruction.md          # Problem statement + metadata
├── task.toml              # Harbor task configuration
├── environment/
│   └── Dockerfile         # Container image specification
├── tests/
│   ├── test.sh            # Test execution script (includes parser)
│   └── config.json        # Full dataset record (for parser)
└── solution/
    └── solve.sh           # Oracle solution (applies gold_patch)
```

**Key transformations:**
- **instruction.md**: Renders problem statement with metadata (repo, version, commit)
- **task.toml**: Sets timeouts and task metadata
- **tests/config.json**: Stores the **entire raw dataset record** for the parser to access
- **tests/test.sh**: Embeds test execution commands + parser script
- **solution/solve.sh**: Creates patch file and applies it

---

## Docker Image Resolution

### Location: `utils.py` → `get_image_names`

**Location:** `utils.py` lines 28-71

The adapter uses a **priority-based fallback system** to determine Docker images:

```python
def get_image_names(samples_hf: list[dict]) -> dict[str, str]:
    id_to_image: dict[str, str] = {}
    for sample in samples_hf:
        # Priority 1: Direct docker_image field
        if "docker_image" in sample:
            id_to_image[instance_id] = sample["docker_image"]
            continue
        
        # Priority 2: Use make_test_spec if in MAP_REPO_VERSION_TO_SPECS
        if repo in MAP_REPO_VERSION_TO_SPECS and version in MAP_REPO_VERSION_TO_SPECS[repo]:
            spec = make_test_spec(sample, namespace="swebench")
            id_to_image[instance_id] = spec.instance_image_key.replace("arm64", "x86_64")
            continue
        
        # Priority 3: Construct from instance_image_tag
        if "instance_image_tag" in sample:
            id_to_image[instance_id] = f"swebench/{instance_id}:{tag}"
        
        # Priority 4: Default fallback
        else:
            id_to_image[instance_id] = f"swebench/{instance_id}:latest"
```

**Priority order:**
1. **`docker_image`** field in dataset (highest priority) - used for custom datasets
2. **`MAP_REPO_VERSION_TO_SPECS`** - standard SWE-bench repos (uses `make_test_spec`)
3. **`instance_image_tag`** - construct image name from tag
4. **Default** - fallback to `swebench/{instance_id}:latest`

**Why this design:**
- Supports custom datasets that specify `docker_image` directly
- Falls back to standard SWE-bench logic for known repos
- Handles both ARM64 and x86_64 architectures (converts ARM64 to x86_64)

**Usage in adapter:**
```python
# adapter.py, lines 194-198
def _build_image_map(self) -> dict:
    """Build mapping from instance_id to Docker image name."""
    records = self.loader.all_records()
    return get_image_names(records)
```

The image map is built once during initialization and reused for all tasks.

---

## Parser Extraction Logic

### Location: `template/test.sh` → Embedded `parser.py` script

**Location:** `template/test.sh` lines 39-105

The parser extraction happens **at runtime** inside the container. The parser script is embedded in `test.sh` and executed after tests run.

#### Step 1: Extract Language and Parser Name

**Location:** `template/test.sh` lines 39-42

```python
# Get language and log_parser_name from spec_dict
spec_dict = datum.get("spec_dict", {})
language = datum.get("language", "Python")
log_parser_name = spec_dict.get("log_parser_name", "pytest")
```

**Data sources (priority order):**
1. `datum["spec_dict"]["log_parser_name"]` - from dataset's `spec_dict`
2. `datum["language"]` - from top-level dataset record
3. Defaults: `language="Python"`, `log_parser_name="pytest"`

#### Step 2: Import LANGUAGE_PARSER_MAP from Your Fork

**Location:** `template/test.sh` line 24

```python
from swebench.harness.log_parsers import LANGUAGE_PARSER_MAP
```

This imports the parser map directly from your SWE-bench fork (installed via the git dependency).

#### Step 3: Get Parser Function

**Location:** `template/test.sh` lines 80-92

```python
# Use LANGUAGE_PARSER_MAP to get parser
if language in LANGUAGE_PARSER_MAP:
    parser_getter = LANGUAGE_PARSER_MAP[language]
    parser = parser_getter(log_parser_name)
else:
    # Fallback to Python parser
    parser_getter = LANGUAGE_PARSER_MAP.get("Python") or LANGUAGE_PARSER_MAP.get("python")
    if parser_getter:
        parser = parser_getter(log_parser_name)
```

**How it works:**
1. `LANGUAGE_PARSER_MAP[language]` returns a **function** (e.g., `get_py_parser_by_name`)
2. That function is called with `log_parser_name` (e.g., `"pytest"`, `"unittest"`, `"custom"`)
3. Returns the actual parser function that parses test output

**Example:**
- `language = "Python"`, `log_parser_name = "pytest"`
- `LANGUAGE_PARSER_MAP["Python"]` → `get_py_parser_by_name`
- `get_py_parser_by_name("pytest")` → `parse_pytest_output` function

#### Step 4: Handle Custom Parsers

**Location:** `template/test.sh` lines 72-79

```python
if log_parser_name == "custom":
    parser_code = spec_dict.get("log_parser_code")
    if parser_code:
        namespace = {}
        exec(parser_code, {}, namespace)
        parser = namespace.get("parse_log_to_json")
```

For custom parsers, the code is stored in `spec_dict["log_parser_code"]` and executed dynamically.

#### Step 5: Parse Test Output

**Location:** `template/test.sh` lines 94-95

```python
if parser:
    eval_status_map = parser(test_output)
```

The parser function takes the raw test output (extracted between `START_TEST_OUTPUT` and `END_TEST_OUTPUT` markers) and returns a dictionary mapping test names to pass/fail status.

**Parser output format:**
```python
{
    "test/test_file.py::test_name": "PASSED",
    "test/test_file.py::test_name2": "FAILED",
    ...
}
```

---

## Key Code Locations

### Core Files

| File | Purpose | Key Functions |
|------|---------|---------------|
| `adapter.py` | Main adapter logic | `SWEBenchTuringLoader`, `SWEBenchTuringToHarbor.generate_task` |
| `utils.py` | Utility functions | `get_image_names`, `get_test_commands` |
| `template/test.sh` | Test execution + parser | Embedded `parser.py` script |
| `template/solve.sh` | Oracle solution | Applies `gold_patch` |
| `template/Dockerfile` | Container image | Uses resolved Docker image |
| `template/instruction.md` | Task description | Renders problem statement |
| `template/task.toml` | Harbor config | Task metadata and timeouts |

### Critical Code Paths

1. **Data Loading**: `adapter.py:70-99` (`_load_data`)
2. **Image Resolution**: `utils.py:28-71` (`get_image_names`)
3. **Test Command Generation**: `utils.py:74-190` (`get_test_commands`)
4. **Parser Installation**: `template/test.sh:9-10` (git dependency)
5. **Parser Extraction**: `template/test.sh:80-92` (LANGUAGE_PARSER_MAP lookup)
6. **Task Generation**: `adapter.py:205-279` (`generate_task`)

### Data Flow

```
Local JSON/JSONL file
    ↓
SWEBenchTuringLoader._load_data()
    ↓
SWEBenchRecord.from_dict() [normalize fields]
    ↓
SWEBenchTuringToHarbor.generate_task()
    ↓
    ├─→ get_image_names() [resolve Docker image]
    ├─→ get_test_commands() [generate test script]
    └─→ Render templates
    ↓
Harbor task directory
    ↓
Container execution
    ↓
test.sh runs → parser.py executes
    ↓
LANGUAGE_PARSER_MAP lookup [from your fork]
    ↓
Parse test output → Generate report.json
```

---

## Summary

The `swebench-turing` adapter:

1. **Uses your SWE-bench fork** by installing it as a git dependency in the embedded parser script
2. **Transforms input** through a three-stage pipeline: file loading → record normalization → Harbor task generation
3. **Gets images** via a priority-based fallback: `docker_image` → `MAP_REPO_VERSION_TO_SPECS` → `instance_image_tag` → default
4. **Extracts parsers** by looking up `LANGUAGE_PARSER_MAP[language](log_parser_name)` from your fork, with fallbacks for custom parsers

The adapter is designed to work with both standard SWE-bench datasets and custom datasets that may not be in `MAP_REPO_VERSION_TO_SPECS`, making it flexible for your specific use case.
