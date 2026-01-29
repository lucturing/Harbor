import os
import platform
import re
import shlex
from pathlib import Path
from textwrap import dedent

from swebench.harness.constants import LATEST
from swebench.harness.test_spec.python import get_test_directives
from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.test_spec.c import make_eval_script_list_c
from swebench.harness.test_spec.java import make_eval_script_list_java
from swebench.harness.test_spec.php import make_eval_script_list_php
from swebench.harness.test_spec.rust import make_eval_script_list_rust
from swebench.harness.test_spec.go import make_eval_script_list_go
from swebench.harness.test_spec.javascript import make_eval_script_list_js
from swebench.harness.test_spec.python import make_eval_script_list_py
from swebench.harness.test_spec.ruby import make_eval_script_list_ruby
from swebench.harness.test_spec.utils import make_eval_script_list_common

extra_build_instance_images_kwargs = {"tag": LATEST}


def read_text(path: Path) -> str:
    """Read text from a file path, raising FileNotFoundError if it doesn't exist."""
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path.read_text()


def render_literal(template_text: str, **repls: str) -> str:
    """Replace only exact placeholders like {key} with provided values."""
    out = template_text
    for k, v in repls.items():
        out = out.replace("{" + k + "}", v)
    return out


def _detect_architecture() -> str:
    """
    Detect the system architecture, matching the logic from swebench harness.
    
    Can be overridden via SWEBENCH_ARCH environment variable.
    Returns 'arm64' or 'x86_64'.
    """
    # Allow override via environment variable
    arch_env = os.getenv("SWEBENCH_ARCH")
    if arch_env:
        if arch_env.lower() in ("arm64", "aarch64"):
            return "arm64"
        elif arch_env.lower() in ("x86_64", "amd64"):
            return "x86_64"
        else:
            raise ValueError(f"Unsupported architecture: {arch_env}")
    
    # Auto-detect from platform (same logic as swebench harness)
    machine = platform.machine()
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    else:
        return "x86_64"


def get_image_names(
    samples_hf: list[dict],
) -> dict[str, str]:
    """
    Return a mapping from sample instance_id → Docker image name
    
    Uses make_test_spec to generate image names in the format:
    sweb.eval.{arch}.{instance_id.lower()}:{tag}
    
    This matches the format produced by prepare_images.py.
    Architecture is auto-detected from the system (can be overridden via SWEBENCH_ARCH env var).
    If docker_image is provided in the sample, it's used (with tag added if missing).
    Otherwise, make_test_spec is used to generate the name.
    """
    # Detect architecture once for all samples
    arch = _detect_architecture()
    
    id_to_image: dict[str, str] = {}
    for sample in samples_hf:
        instance_id = sample["instance_id"]
        
        # If docker_image is explicitly provided, use it as-is (no arch modification)
        if "docker_image" in sample and sample["docker_image"]:
            docker_image = sample["docker_image"]
            # Ensure it has a tag (add :latest if missing)
            if ":" not in docker_image:
                docker_image = f"{docker_image}:latest"
            id_to_image[instance_id] = docker_image
            continue
        
        # Use make_test_spec to generate the image name (matches prepare_images.py)
        try:
            # Get instance_image_tag from sample if available, otherwise use LATEST
            instance_image_tag = sample.get("instance_image_tag", LATEST)
            # Get namespace if available (for remote images)
            namespace = sample.get("namespace")
            
            spec = make_test_spec(
                sample, 
                namespace=namespace,
                instance_image_tag=instance_image_tag,
                arch=arch  # Use detected architecture
            )
            # Generate image name: sweb.eval.{arch}.{instance_id.lower()}:{tag}
            # This matches the format from prepare_images.py
            image_name = spec.instance_image_key
            # The instance_id is already lowercased by make_test_spec
            id_to_image[instance_id] = image_name
        except (KeyError, Exception) as e:
            # Fallback: construct basic image name matching the format
            tag = sample.get("instance_image_tag", LATEST)
            id_to_image[instance_id] = f"sweb.eval.{arch}.{instance_id.lower()}:{tag}"
    
    return id_to_image


def _has_new_file_in_patch(diff_text: str) -> bool:
    """
    Returns True if at least one file was newly added in the diff patch.
    A file is considered added if it contains '--- /dev/null' and '+++ b/<file>'.
    """
    # Split patch into chunks for each file
    chunks = re.split(r'^diff --git ', diff_text, flags=re.MULTILINE)

    for chunk in chunks:
        if not chunk.strip():
            continue

        # Reattach the removed line prefix
        chunk = "diff --git " + chunk

        # Check for the "new file mode" marker or "--- /dev/null"
        is_new_file = re.search(r'^new file mode \d+', chunk, flags=re.MULTILINE)
        has_dev_null = re.search(r'^--- /dev/null$', chunk, flags=re.MULTILINE)
        has_added_file = re.search(r'^\+\+\+ b/.*', chunk, flags=re.MULTILINE)

        if (is_new_file or has_dev_null) and has_added_file:
            return True

    return False


def get_test_commands(
    test_patch: str, repo: str, version: str, base_commit: str, spec_dict: dict | None = None, task_dict: dict | None = None
) -> str:
    """Creates a script which runs the tests using make_eval_script_list from the harness.
    
    This replicates the harness logic exactly, using language-specific eval script generators.
    
    Args:
        test_patch: The test patch to apply
        repo: Repository name
        version: Version string
        base_commit: Base commit hash
        spec_dict: Optional spec_dict from dataset
        task_dict: Optional full task dict (must include 'language' field)
    """
    if not task_dict:
        raise ValueError("task_dict is required to use make_eval_script_list")
    
    conda_env = "testbed"
    repo_directory = "/testbed"
    
    # Get language and map to extension (matching workflow.py)
    language = task_dict.get("language", "Python")
    LANGUAGES_STR_MAP = {
        "C": "c",
        "C++": "c",
        "Go": "go",
        "Java": "java",
        "JavaScript": "js",
        "TypeScript": "ts",
        "PHP": "php",
        "Python": "py",
        "Ruby": "rb",
        "Rust": "rs",
        "C#": "cs"
    }
    ext = LANGUAGES_STR_MAP.get(language, "py")
    
    # Select the appropriate make_eval_script_list function
    func = {
        "c": make_eval_script_list_c,
        "rust": make_eval_script_list_rust,
        "php": make_eval_script_list_php,
        "js": make_eval_script_list_js,
        "ts": make_eval_script_list_js,
        "py": make_eval_script_list_py,
        "java": make_eval_script_list_java,
        "go": make_eval_script_list_go,
        "rb": make_eval_script_list_ruby
    }.get(ext, make_eval_script_list_common)
    
    # Check if test patch has new files (affects run_all_tests flag)
    has_new_test_file = _has_new_file_in_patch(test_patch)
    
    # Use spec_dict if provided, otherwise use empty dict
    specs = spec_dict if spec_dict else {}
    
    # Call make_eval_script_list with the same parameters as workflow.py
    # The harness functions use the task_dict and specs directly, they don't need
    # the repo to be in MAP_REPO_VERSION_TO_SPECS if specs is provided
    eval_commands = func(
        task_dict,
        specs,
        conda_env,
        repo_directory,
        base_commit,
        test_patch,
        run_all_tests=has_new_test_file
    )
    
    # Join commands with newlines to create the full script
    # The harness eval scripts from make_eval_script_list already include:
    # - cd to repo directory
    # - conda activation (if needed)
    # - git checkout/reset operations
    # - test patch application
    # - test execution (with START_TEST_OUTPUT/END_TEST_OUTPUT markers if the harness adds them)
    # - cleanup/reset operations
    #
    # We just need to wrap with LOG_FILE tracking so the parser can read the output
    script_content = dedent("""#!/bin/bash
set -uo pipefail -x

# Setup LOG_FILE for parser (parser.py will add markers if missing)
LOG_FILE=$(mktemp)
export LOG_FILE
exec 3>&1 4>&2
exec > >(tee "$LOG_FILE") 2>&1

""")
    
    # Add the eval commands from make_eval_script_list
    # These commands already include all the harness logic
    script_content += "\n".join(eval_commands)
    
    # Restore stdout/stderr
    script_content += dedent("""
# Restore stdout/stderr
exec 1>&3 2>&4
""")
    
    return script_content
