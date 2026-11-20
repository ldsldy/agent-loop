#!/usr/bin/env bash
# 조각 PR 하나를 최소 리뷰 → 머지 → 정리까지 잇는다. 절차: loop 스킬 "세션 운영".
# 조각 PR(→loop)은 CI를 돌리지 않는다. CI는 루프 끝 기준 브랜치 PR에서만 돈다.
#   bash <플러그인>/scripts/pr_chain.sh <루프 이름> <PR 번호>     # 저장소 루트에서, 보통 백그라운드로
# - 최소 리뷰가 pass가 아니면 머지하지 않는다(minimal=...). 테스트 파일만 바뀐 PR(config.test_globs)은 리뷰 없이 머지한다.
# - 리뷰는 전용 worktree(.claude/worktrees/review)에서 돌린다. 원래 체크아웃은 편집기가 열고 있을 수 있어 바꾸지 않는다.
# - 여러 개를 동시에 돌려도 리뷰는 잠금(.git/review.lock)으로 하나씩 돈다. 비정상 종료로 잠금이 남으면 지운다.
# - 브랜치 삭제는 머지를 확인한 뒤에만 한다. 그 브랜치를 쓰던 worktree는 지우지 않고 detach한다(구현 세션이 계속 쓴다).
U=$1; PR=$2; L=loop/$U
HERE="$(cd "$(dirname "$0")" && pwd)"
MAIN="$(git rev-parse --show-toplevel)"; REV="$MAIN/.claude/worktrees/review"; LOCK="$(git rev-parse --git-common-dir)/review.lock"
[ -d "$REV" ] || git worktree add -q --detach "$REV" origin/$L
SHA=$(gh pr view $PR --json headRefOid -q .headRefOid)
HEAD_REF=$(gh pr view $PR --json headRefName -q .headRefName)
echo "PR=$PR sha=${SHA:0:7}"
# 테스트 파일만 바뀐 PR은 최소 리뷰 없이 머지한다(loop-review 스킬 "건너뛰는 변경").
FILES=$(gh pr view $PR --json files -q '.files[].path') || exit 1
[ -n "$FILES" ] || { echo "파일 목록을 읽지 못함"; exit 1; }  # 빈 목록을 "테스트만"으로 보지 않는다
if [ "$(printf '%s\n' "$FILES" | python -I "$HERE/scope.py" tests-only)" = true ]; then
  echo "minimal=skipped(tests only)"
  gh pr comment $PR -b "## 최소 리뷰: 생략 (${SHA:0:7})
테스트 파일만 바뀌어 리뷰하지 않는다(loop-review 스킬 \"건너뛰는 변경\"). 테스트 근거는 루프 끝 기준 브랜치 PR의 CI." >/dev/null
else
until mkdir "$LOCK" 2>/dev/null; do sleep 20; done
trap 'rmdir "$LOCK"' EXIT
git -C "$REV" fetch -q origin && git -C "$REV" switch -q --detach $SHA
( cd "$REV" && python -I "$HERE/review.py" --level minimal --base origin/$L --pr $PR 2>&1 | tail -4 )
VERDICT="$REV/ci-results/quality/${SHA:0:7}-minimal/verdict.json"
mkdir -p "$MAIN/ci-results/quality" && cp -r "$REV/ci-results/quality/${SHA:0:7}-minimal" "$MAIN/ci-results/quality/" 2>/dev/null
V=$(python -I -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['verdict'])" "$VERDICT"); echo "minimal=$V"
[ "$V" = pass ] || exit 0
gh pr comment $PR -b "## 최소 리뷰: 통과 (${SHA:0:7})
최소 리뷰 항목 모두 pass(조각 PR은 CI 없음, 테스트 근거는 루프 끝 기준 브랜치 PR의 CI). 결과 원본: \`ci-results/quality/${SHA:0:7}-minimal/\`" >/dev/null
fi
gh pr merge $PR --squash || exit 0
git fetch -q origin
# 구현 세션이 worktree를 계속 쓰므로 worktree는 지우지 않는다. detach한 뒤 브랜치만 지운다.
WT=$(git worktree list --porcelain | awk -v b="branch refs/heads/$HEAD_REF" '/^worktree /{w=substr($0,10)} $0==b{print w}')
[ -n "$WT" ] && git -C "$WT" switch -q --detach && echo "detach: $WT"
git branch -D "$HEAD_REF" >/dev/null 2>&1; git push -q origin --delete "$HEAD_REF" 2>/dev/null; echo "cleanup: $HEAD_REF"
( cd "$MAIN" && python -I "$HERE/orchestrator.py" next --unit $U | grep -E '"action"|"slice"' | paste - - | head -12 )
