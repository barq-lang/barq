#!/bin/bash
# Times each benchmark on the tree-walker and on the VM (best of 3 runs).
# Usage: benchmarks/run.sh   (from the repo root)
cd "$(dirname "$0")/.."
best() {
  local b=999
  for _ in 1 2 3; do
    local s=$(date +%s.%N)
    "$@" > /dev/null
    local t=$(echo "$(date +%s.%N) - $s" | bc)
    b=$(echo "if ($t < $b) $t else $b" | bc)
  done
  printf "%.2f" "$b"
}
printf "%-16s %10s %10s %8s\n" benchmark tree-walk vm speed-up
for f in benchmarks/*.bq; do
  ./bin/barq compile "$f" > /dev/null
  t=$(best ./bin/barq run --interp "$f")
  v=$(best ./bin/barq run "$f")
  printf "%-16s %9ss %9ss %7.1fx\n" "$(basename "$f" .bq)" "$t" "$v" "$(echo "$t / $v" | bc -l)"
done
