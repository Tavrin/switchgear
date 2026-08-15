# independent-review predicates
# shellcheck shell=bash

compute_independence() {
  local subject_model_json="$1" reviewer_model_json="$2" subject_job="$3" reviewer_job="$4"
  jsonutil independence "$subject_model_json" "$reviewer_model_json" "$subject_job" "$reviewer_job"
}

unmet_required() {
  local indep_json_file="$1"
  jsonutil required-unmet "$AI_OPS_PROFILE_FILE" "$indep_json_file"
}

attach_review_to_subject() {
  local subject_job="$1" reviewer_job="$2" review_json="$3"
  local sdir
  sdir=$(job_dir "$subject_job")
  [ -d "$sdir" ] || refuse "subject job not found: $subject_job"
  cp -- "$review_json" "$sdir/review.json"
  python3 - "$sdir/result.json" "$review_json" <<'PY'
import json, sys
result_path, review_path = sys.argv[1], sys.argv[2]
result = json.load(open(result_path, encoding="utf-8"))
review = json.load(open(review_path, encoding="utf-8"))
result["review"] = review
unmet = review.get("required_unmet") or []
if unmet:
    result["status"] = "review_failed"
else:
    result["status"] = "ok"
json.dump(result, open(result_path, "w", encoding="utf-8"), indent=2)
open(result_path, "a", encoding="utf-8").write("\n")
PY
}
