# BENCHMARK DATA SHOULD NEVER testbedEAR IN TRAINING CORPORA.

{test_commands}

cd ..

# Ensure Python is available for parser (required for all languages)
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required for test evaluation but not found in the container" >&2
    mkdir -p /logs/verifier
    echo 0 > /logs/verifier/reward.txt
    exit 1
fi

cat > parser.py <<EOF
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "swebench @ git+https://{github_token}@github.com/TuringGpt/Microsoft-SWE-Bench.git",
#     "datasets==2.16.1",
#     "fastcore<1.11"
# ]
# ///

import sys
import json
import os
from swebench.harness.constants import ResolvedStatus, EvalType, PASS_TO_PASS, \
                                       FAIL_TO_PASS, KEY_INSTANCE_ID, \
                                       START_TEST_OUTPUT, END_TEST_OUTPUT
from swebench.harness.grading import get_eval_tests_report, get_resolution_status

from swebench.harness.log_parsers import LANGUAGE_PARSER_MAP

with open("/tests/config.json", "r") as file:
    datum = json.load(file)

report_map = {}
instance_id = datum[KEY_INSTANCE_ID]
report_map[instance_id] = {
    "patch_is_None": False,
    "patch_exists": False,
    "patch_successfully_applied": False,
    "resolved": False,
}
report_map[instance_id]["patch_exists"] = True

# Get language and log_parser_name from spec_dict
spec_dict = datum.get("spec_dict", {})
language = datum.get("language", "Python")
log_parser_name = spec_dict.get("log_parser_name", "pytest")

# Get fail_to_pass and pass_to_pass from datum
fail_to_pass = datum.get("fail_to_pass", [])
pass_to_pass = datum.get("pass_to_pass", [])

test_log_path = os.environ["LOG_FILE"]

# Read test output and add markers if not present
with open(test_log_path, 'r+') as f:
    content = f.read()
    if START_TEST_OUTPUT not in content:
        f.seek(0)
        f.write(f'{START_TEST_OUTPUT}\n{content}\n{END_TEST_OUTPUT}')
        f.truncate()
        f.seek(0)  # Seek back to beginning before reading
        content = f.read()

# Extract test output between markers
start_idx = content.find(START_TEST_OUTPUT)
end_idx = content.find(END_TEST_OUTPUT)
if start_idx != -1 and end_idx != -1:
    test_output = content[start_idx + len(START_TEST_OUTPUT):end_idx].strip()
    report_map[instance_id]["patch_successfully_applied"] = True
else:
    test_output = content
    report_map[instance_id]["patch_successfully_applied"] = False

# Parse test output using language-specific parser
try:
    if log_parser_name == "custom":
        parser_code = spec_dict.get("log_parser_code")
        if parser_code:
            namespace = {}
            exec(parser_code, {}, namespace)
            parser = namespace.get("parse_log_to_json")
        else:
            parser = None
    else:
        # Use LANGUAGE_PARSER_MAP to get parser (same as workflow.py)
        # LANGUAGE_PARSER_MAP[language] returns a function that takes log_parser_name
        if language in LANGUAGE_PARSER_MAP:
            parser_getter = LANGUAGE_PARSER_MAP[language]
            parser = parser_getter(log_parser_name)
        else:
            # Fallback to Python parser
            parser_getter = LANGUAGE_PARSER_MAP.get("Python") or LANGUAGE_PARSER_MAP.get("python")
            if parser_getter:
                parser = parser_getter(log_parser_name)
            else:
                parser = None
    
    if parser:
        eval_status_map = parser(test_output)
    else:
        # Fallback: empty status map
        eval_status_map = {}
        report_map[instance_id]["patch_successfully_applied"] = False
except Exception as e:
    print(f"Error parsing test output: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    eval_status_map = {}
    report_map[instance_id]["patch_successfully_applied"] = False

# Create evaluation report
eval_ref = {
    KEY_INSTANCE_ID: instance_id,
    FAIL_TO_PASS: fail_to_pass,
    PASS_TO_PASS: pass_to_pass,
}

# Use PASS_AND_FAIL eval type (can be customized based on repo if needed)
eval_type = EvalType.PASS_AND_FAIL

report = get_eval_tests_report(
    eval_status_map, eval_ref, eval_type=eval_type
)

if get_resolution_status(report) == ResolvedStatus.FULL.value:
    report_map[instance_id]["resolved"] = True

report_map[instance_id]["tests_status"] = report

json.dump(report_map, open("/logs/verifier/report.json", "w"), indent=4)

print(f"SWEBench results starts here")
if report_map[instance_id]["resolved"]:
    print("PASSED")
else:
    print("FAILED")
print("SWEBench results ends here")
sys.exit(0 if report_map[instance_id]["resolved"] else 1)
EOF

chmod +x parser.py

# Install dependencies and run parser
set +e

# Try to use uv if available, otherwise use pip
if command -v uv >/dev/null 2>&1; then
    # uv is available, use it
    uv run parser.py | tee -a "$LOG_FILE"
    exit_code=$?
else
    # uv not available, install dependencies with pip and run with python3
    python3 -m pip install --quiet --user \
        "swebench @ git+https://{github_token}@github.com/TuringGpt/Microsoft-SWE-Bench.git" \
        "datasets==2.16.1" \
        "fastcore<1.11" >/dev/null 2>&1 || true
    python3 parser.py | tee -a "$LOG_FILE"
    exit_code=$?
fi

set -e

# ----------------------- Reward logging for Harbor ------------------------
mkdir -p /logs/verifier
if [ "${exit_code}" -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi

exit "${exit_code}"
