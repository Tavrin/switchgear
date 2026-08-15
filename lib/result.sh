# job state directory + result/meta writers
# shellcheck shell=bash

job_dir() {
  echo "$STATE/jobs/$1"
}

init_job() {
  local job_id="$1"
  mkdir -p "$(job_dir "$job_id")"
}

write_result_json() {
  local dest="$1"
  python3 -c 'import json,sys; json.dump(json.loads(sys.stdin.read()), open(sys.argv[1],"w"), indent=2)' "$dest"
  echo >> "$dest"
}

print_job_stdout() {
  local model="$1" dir="$2" rc="$3" job="$4"
  local jd
  jd=$(job_dir "$job")
  echo "model=$model"
  echo "dir=$dir"
  echo "exit=$rc"
  echo "result=$jd/events.json"
  echo "meta=$jd/meta"
  echo "job=$job"
}
